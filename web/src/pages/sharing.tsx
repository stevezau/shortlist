import { EyeOff, Info, RefreshCw, ScanEye, ShieldCheck, ShieldX } from "lucide-react";
import { type ReactNode, useState } from "react";
import { Link, useNavigate } from "react-router";

import { MutationAlert } from "@/components/mutation-alert";
import { PageHeader } from "@/components/page-header";
import { EmptyState, QueryBoundary } from "@/components/query-boundary";
import { SaveStatus } from "@/components/save-status";
import { UserAvatar } from "@/components/user-avatar";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { settingBool } from "@/lib/format";
import { rowsNotHidden, rowsNotTheirs } from "@/lib/privacy-attention";
import { type GridCell, accountHasProblem, accountSummary, gridCell, rowColumnName } from "@/lib/privacy-grid";
import { usePrivacyStatus, useSaveSettings, useSettings, useStartRun } from "@/lib/queries";
import type { AccountPrivacy, PrivacyStatus } from "@/lib/types";
import { USER_TYPE_LABEL, profileLabel } from "@/lib/user-profile";
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
    <div className="space-y-6 [overflow-wrap:anywhere]">
      <PageHeader
        title="Privacy"
        // Promise first, mechanism second: what they came for is whether the hiding holds, and that
        // it is a live reading. The owner's own view — Plex cannot filter it — is a fact row under
        // Policy rather than a clause in this line.
        subtitle="Who can see which row on Plex, read live from plex.tv."
        actions={
          <Button variant="outline" onClick={() => void query.refetch()} loading={query.isFetching}>
            {!query.isFetching && <RefreshCw aria-hidden="true" />}
            Read again
          </Button>
        }
      />
      {/* Proof first: whether Plex applies the rules is the question the grid below can't answer, so it
          leads. It comes from the last run's own measurement and stays reachable when plex.tv is down. */}
      {query.data && <EnforcementPanel data={query.data} />}
      <QueryBoundary
        query={query}
        skeleton={
          <div className="space-y-3">
            <Skeleton className="h-20 w-full" />
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
            <AccountsGrid data={data} />
          </div>
        )}
      </QueryBoundary>
      {/* Outside the live read on purpose: a server-wide setting must stay reachable when plex.tv
          is down, which is exactly when an owner comes here. */}
      <PolicyPanel />
      {/* Shortlist's own record, so it stays true when plex.tv cannot be read. */}
      {query.data && (
        <p className="text-sm text-muted-foreground">
          {query.data.snapshots_kept} {query.data.snapshots_kept === 1 ? "snapshot" : "snapshots"} kept
          &middot; restored on uninstall
        </p>
      )}
    </div>
  );
}

/**
 * Server-wide rules for who sees what. The switch saves the moment it is flipped (the same key and
 * the same save it had under Settings › Advanced), and applies on the next run.
 */
function PolicyPanel() {
  const settings = useSettings();
  const save = useSaveSettings();
  const [saved, setSaved] = useState(false);
  const flip = (on: boolean) => {
    setSaved(false);
    save.mutate({ "privacy.hide_shared_from_disabled": on }, { onSuccess: () => setSaved(true) });
  };

  return (
    <Card aria-labelledby="privacy-policy-title" role="region">
      <CardHeader className="pb-3">
        <CardTitle id="privacy-policy-title" role="heading" aria-level={2}>
          Policy
        </CardTitle>
      </CardHeader>
      <div className="divide-y border-t">
        <div className="space-y-2 px-6 py-4">
          <div className="flex items-start justify-between gap-5">
            <div className="min-w-0 space-y-1">
              <p className="font-medium">Disabled people see no rows at all</p>
              <p className="max-w-prose text-sm text-muted-foreground">
                On: a disabled account also loses &ldquo;Popular on this server&rdquo;. Off: it still sees
                shared rows. Applies on the next run.
              </p>
            </div>
            {settings.isPending ? (
              <Skeleton className="h-6 w-11 shrink-0 rounded-full" />
            ) : settings.isError ? null : (
              <Switch
                className="shrink-0"
                checked={settingBool(settings.data, "privacy.hide_shared_from_disabled", true)}
                onCheckedChange={flip}
                disabled={save.isPending}
                aria-label="Hide shared rows from disabled users"
              />
            )}
          </div>
          {settings.isError && (
            <MutationAlert
              error={settings.error}
              fallback="Couldn’t read this setting. Try again."
              onRetry={() => void settings.refetch()}
            />
          )}
          <SaveStatus
            isPending={save.isPending}
            isError={save.isError}
            error={save.error}
            saved={saved}
            onRetry={() => save.variables && flip(save.variables["privacy.hide_shared_from_disabled"] === true)}
          />
        </div>
        <div className="flex items-start justify-between gap-5 px-6 py-4">
          <div className="min-w-0 space-y-1">
            <p className="font-medium">Your own account sees every row</p>
            <p className="max-w-prose text-sm text-muted-foreground">
              Plex never filters the account that owns the server.
            </p>
          </div>
          <Badge className="shrink-0 font-medium">Plex limit</Badge>
        </div>
      </div>
    </Card>
  );
}

/** Verdicts that mean an account can see rows that aren't theirs, said by the grid per account. */
const EXPOSED_SUMMARIES = ["missing", "filter_unreadable", "unhideable"];

/** Accounts the reading says are not hiding, at least one for a verdict that names none. */
function exposedAccounts(data: PrivacyStatus): number {
  const count = data.accounts.filter(
    (account) =>
      account.state === "missing" || account.state === "unreadable_filter" || rowsNotHidden(account, data) > 0,
  ).length;
  return Math.max(count, 1);
}

/**
 * What the page says when the reading itself cannot be trusted or has nothing to show.
 *
 * Only failures of the read and the "no rows yet" case live here. Everything about a specific
 * account — a missing rule, a refused one, an unreadable filter — is said once, on that account's
 * line in the grid, so the same explanation is never repeated in a banner above it. Never green off a
 * failed read, and never silent about a verdict this build does not know.
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
      <Banner
        tone="bad"
        role="alert"
        action={
          <Button variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        }
      >
        <p>
          Couldn't read your Plex sharing settings from plex.tv, so nothing
          below is current. {data.error}
        </p>
      </Banner>
    );
  }
  if (data.summary === "rows_unknown") {
    return (
      <Banner
        tone="bad"
        role="alert"
        action={
          <Button variant="outline" size="sm" onClick={onRetry}>
            Try again
          </Button>
        }
      >
        <p>
          Couldn't read your rows from Plex, so there's nothing to check the
          filters against. The filters below are real — what's unknown is
          whether they cover every row. {data.rows_error}
        </p>
      </Banner>
    );
  }
  if (data.summary === "clean" && data.rows_on_plex.length === 0) {
    return (
      <Banner tone="neutral">
        <p>
          No per-person rows exist on Plex yet, so there's nothing for anyone to
          hide. <ReadAt at={data.read_at} />
        </p>
      </Banner>
    );
  }
  // The grid says these account by account, but a clean-looking panel or a quiet top of page must
  // never sit above an exposure, so say it once in a line here.
  if (EXPOSED_SUMMARIES.includes(data.summary)) {
    const exposed = exposedAccounts(data);
    return (
      <Banner tone="bad" role="alert">
        <p>
          {exposed === 1 ? "1 account isn’t" : `${exposed} accounts aren’t`} hiding every row that isn’t theirs.
          The accounts below say which.
        </p>
      </Banner>
    );
  }
  // Verdicts the grid and the enforcement panel already say, account by account.
  if (["clean", "not_enforced"].includes(data.summary)) {
    return null;
  }
  // A verdict this build does not know. Reached only if the server grows a seventh state and nobody
  // wires it up here — and the default has to be "I don't know", never the green banner. Falling
  // through to reassurance is the one direction this page's whole docstring says it must not take.
  //
  // The raw verdict is NOT interpolated into the sentence any more. `data.summary` is an internal
  // enum ("rows_unknown", "not_enforced"), and dropping one mid-paragraph put a snake_case token in
  // front of an owner as if it were English. It is still shown — a mismatch between server and SPA
  // is exactly the thing a bug report needs — but labelled as a code to quote, not as prose.
  return (
    <Banner tone="neutral">
      <div className="space-y-1">
        <p>
          This version of Shortlist doesn&rsquo;t recognise the verdict your
          server sent back, so it isn&rsquo;t saying whether anything is hidden.
          The accounts below are still a live read. <ReadAt at={data.read_at} />
        </p>
        <p className="text-muted-foreground">
          Update Shortlist; if it keeps happening, report it and quote this
          code: <code className="font-mono">{data.summary}</code>
        </p>
      </div>
    </Banner>
  );
}

const BANNER_ICON = { good: ShieldCheck, bad: ShieldX, neutral: Info } as const;

function Banner({
  tone,
  role,
  action,
  children,
}: {
  tone: "good" | "bad" | "neutral";
  role?: string;
  /** A button beside the text (a retry), kept out of the paragraph so the sentence reads whole. */
  action?: ReactNode;
  children: ReactNode;
}) {
  const Icon = BANNER_ICON[tone];
  return (
    <div
      role={role}
      className={cn(
        "flex flex-col items-start gap-3 rounded-lg border px-3.5 py-3 text-sm sm:flex-row sm:items-center sm:justify-between",
        tone === "bad" && "border-destructive/40 bg-destructive/10",
        tone === "good" && "border-success/40 bg-success/10",
        tone === "neutral" && "bg-muted/40",
      )}
    >
      <div className="grid min-w-0 grid-cols-[auto_minmax(0,1fr)] gap-2.5">
        <Icon
          className={cn(
            "mt-0.5 h-4 w-4",
            tone === "bad" && "text-destructive-text",
            tone === "good" && "text-success",
            tone === "neutral" && "text-muted-foreground",
          )}
          aria-hidden="true"
        />
        <div className="min-w-0 space-y-1">{children}</div>
      </div>
      {action}
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

/** The status line under an account's name, in the owner's terms. The evidence is in the sentence, never a bare tick. */
function accountNote(account: AccountPrivacy, exposed: number): { text: string; tone: string } | null {
  switch (account.state) {
    case "missing":
      return {
        text: "Missing hide rules. The next run merges them back in; if they keep disappearing, something else is rewriting this share filter.",
        tone: "text-destructive-text",
      };
    case "unreadable_filter":
      return {
        text: "Plex can’t read this account’s restrictions: a label has an “&” in its name. Rename it in Plex and the next run can hide their view.",
        tone: "text-destructive-text",
      };
    case "refused_by_plex":
      return {
        text: `${
          exposed > 0 ? `${account.display_name} ${rowsNotTheirs(exposed)}. ` : ""
        }Plex rejects hide rules for Restriction Profiles (${profileLabel(account.restriction_profile)}). Clear it in Plex to fix; Shortlist never changes that profile for you.`,
        tone: "text-warning",
      };
    case "left_alone":
      return {
        text: "You asked Shortlist to leave this account’s Plex sharing alone, so it hides nothing.",
        tone: "text-muted-foreground",
      };
    case "owner":
      return {
        text: "Plex has no share to filter for you. To see what your users see, watch on a non-owner account.",
        tone: "text-muted-foreground",
      };
    case "unknown":
      return { text: "Not checked.", tone: "text-muted-foreground" };
    default:
      return null;
  }
}

function Cell({ kind }: { kind: GridCell }) {
  switch (kind) {
    case "own":
      return <Badge className="bg-raised font-semibold">Own</Badge>;
    case "hidden":
      return (
        <span className="inline-flex items-center gap-1.5 text-xs text-muted-foreground">
          <EyeOff className="size-3.5 shrink-0" aria-hidden="true" />
          Hidden
        </span>
      );
    case "sees":
      return <Badge variant="warning" className="font-semibold">Sees it</Badge>;
    case "stored_not_applied":
      return <Badge variant="warning" className="font-semibold">Rule stored, not applied</Badge>;
    case "refused":
      return <span className="text-xs text-muted-foreground">Rule refused by Plex</span>;
    case "owner":
      return <span className="text-xs text-muted-foreground">Sees all</span>;
    case "left_alone":
      return <span className="text-xs text-muted-foreground">Left alone</span>;
    default:
      return <span className="text-xs text-muted-foreground">Not checked</span>;
  }
}

/** The one-line answer for an account, from its cell counts. Nothing reads as hiding while any cell says otherwise. */
function AccountSummaryLine({ account, data }: { account: AccountPrivacy; data: PrivacyStatus }) {
  const muted = "text-sm text-muted-foreground";
  if (account.state === "owner") return <span className={muted}>Sees every row</span>;
  if (account.state === "left_alone") return <span className={muted}>Left alone</span>;
  if (account.state === "unknown") return <span className={muted}>Not checked</span>;

  const counts = accountSummary(account, data);
  if (counts.sees > 0) {
    return (
      <Badge variant="warning" className="font-semibold">
        Sees {counts.sees} {counts.sees === 1 ? "row" : "rows"} that aren’t theirs
      </Badge>
    );
  }
  if (counts.stored_not_applied > 0) {
    return (
      <Badge variant="warning" className="font-semibold">
        {counts.stored_not_applied} {counts.stored_not_applied === 1 ? "rule" : "rules"} stored, not applied
      </Badge>
    );
  }
  if (counts.refused > 0) return <span className={muted}>Rule refused by Plex</span>;
  if (counts.hidden === 0) {
    return <span className={muted}>{counts.unknown > 0 ? "Not checked" : "No other rows to hide"}</span>;
  }
  return (
    <span className={cn(muted, "inline-flex items-center gap-1.5")}>
      <EyeOff className="size-4 shrink-0" aria-hidden="true" />
      {counts.unknown > 0
        ? `Hides ${counts.hidden} other ${counts.hidden === 1 ? "row" : "rows"} · ${counts.unknown} not checked`
        : `Hides all ${counts.hidden} other ${counts.hidden === 1 ? "row" : "rows"}`}
    </span>
  );
}

/**
 * Who sees what: one line per account, problems first.
 *
 * Counts ROWS, the owner's unit (one per person however many libraries it spans). Collections, one
 * per library, are a different and larger number and are not shown here. A server has dozens of
 * accounts and dozens of rows, so a grid of both cannot fit; each account says its verdict once and
 * keeps the per-row cells behind a toggle.
 */
function AccountsGrid({ data }: { data: PrivacyStatus }) {
  const problems = data.accounts.filter((account) => accountHasProblem(account, data));
  const rest = data.accounts.filter((account) => !accountHasProblem(account, data));
  const readAt = new Date(data.read_at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle role="heading" aria-level={2}>
          Who sees what
        </CardTitle>
        <CardDescription>One line per Plex account, problems first. Read from plex.tv at {readAt}.</CardDescription>
      </CardHeader>
      <ul aria-label="Plex accounts">
        {[...problems, ...rest].map((account) => (
          <AccountRow key={account.account_id} account={account} data={data} />
        ))}
      </ul>
    </Card>
  );
}

function AccountRow({ account, data }: { account: AccountPrivacy; data: PrivacyStatus }) {
  const [filtersOpen, setFiltersOpen] = useState(false);
  // null = follow the default: open when this account sees something, so an exposure never hides behind a toggle.
  const [rowsToggled, setRowsToggled] = useState<boolean | null>(null);
  const counts = accountSummary(account, data);
  const rowsOpen = rowsToggled ?? (counts.sees > 0 || counts.stored_not_applied > 0);
  const exposed = rowsNotHidden(account, data);
  const note = accountNote(account, exposed);
  // Where Plex refuses hide rules, the person's own page walks through the two-step fix in Plex.
  const fixLink =
    account.state === "refused_by_plex" && account.user_id !== null ? (
      <>
        {" "}
        <Link to={`/users/${account.user_id}`} className="font-medium underline underline-offset-2">
          How &rarr;
        </Link>
      </>
    ) : null;
  const rows = data.rows_on_plex
    .map((label) => ({ label, kind: gridCell(account, label, data) }))
    .sort((a, b) => Number(a.kind === "hidden") - Number(b.kind === "hidden"));
  // The owner, a left-alone account and an unread one say the same thing for every row, so a list
  // of 46 identical cells adds nothing.
  const perRow = rows.length > 0 && !["owner", "left_alone", "unknown"].includes(account.state);
  const linkButton =
    "text-sm font-medium text-primary underline-offset-4 hover:underline focus-visible:underline focus-visible:outline-none";

  return (
    <li className="space-y-2 border-t px-6 py-3 first:border-t-0">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2">
        <div className="flex min-w-0 flex-1 basis-64 items-start gap-3">
          <UserAvatar name={account.display_name} />
          <div className="min-w-0">
            <p className="font-semibold">
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
              {account.state === "owner" && (
                <span className="ml-2 rounded-full border border-border-strong px-2 py-0.5 align-middle text-xs font-medium text-muted-foreground">
                  Plex limit
                </span>
              )}
            </p>
            {/* Shared or Managed decides what Plex lets an owner restrict, so it sits by the name. Not
                for an account Shortlist has not synced, whose kind is only a fallback (see the note). */}
            <p className="text-xs text-muted-foreground">
              {account.user_id !== null && USER_TYPE_LABEL[account.user_type]}
              {account.restriction_profile && ` · Restriction Profile ${profileLabel(account.restriction_profile)}`}
            </p>
            {account.user_id === null && (
              // A share added since the last user sync. Every attribute beside the filter is a
              // fallback (`user_type` reads "shared", `restriction_profile` reads ""), so the state
              // beside it may be wrong in the one direction that matters — a parental-profile account
              // would be badged "missing hide rules" when Plex simply refuses them.
              <p className="mt-1.5 text-sm text-muted-foreground">
                Plex knows this account, Shortlist hasn&rsquo;t synced it yet. Run Sync users on the Users
                page for a reliable answer.
              </p>
            )}
          </div>
        </div>
        {/* The verdict and the toggles share a column beside the name, so a healthy account is one
            line tall: with 48 accounts, a line of toggles under each made the page 5,700px. */}
        <div className="flex shrink-0 flex-col gap-1 sm:items-end">
          <AccountSummaryLine account={account} data={data} />
          <div className="flex flex-wrap gap-x-4 gap-y-1">
            {account.other_conditions.length > 0 && (
              <button
                type="button"
                onClick={() => setFiltersOpen((value) => !value)}
                aria-expanded={filtersOpen}
                className={linkButton}
              >
                {filtersOpen ? "Hide" : "Show"} their own filters ({account.other_conditions.length})
              </button>
            )}
            {perRow && (
              <button
                type="button"
                onClick={() => setRowsToggled(!rowsOpen)}
                aria-expanded={rowsOpen}
                className={linkButton}
              >
                {rowsOpen ? "Hide" : "Show"} each row ({rows.length})
              </button>
            )}
          </div>
        </div>
      </div>
      {note && (
        <p className={cn("max-w-3xl text-sm", note.tone)}>
          {note.text}
          {fixLink}
        </p>
      )}
      {filtersOpen && (
        <ul className="space-y-0.5">
          {/* Indexed key: a filter can legitimately repeat a condition string, and two
              identical keys make React drop one of them — on the list whose whole job is to
              show the owner that we preserved their filters exactly. */}
          {account.other_conditions.map((condition, index) => (
            <li key={`${index}-${condition}`} className="break-all font-mono text-xs text-muted-foreground">
              {condition}
            </li>
          ))}
        </ul>
      )}
      {perRow && rowsOpen && (
        <ul className="grid gap-x-6 gap-y-2 pt-1 sm:grid-cols-2 lg:grid-cols-3">
          {rows.map(({ label, kind }) => (
            <li key={label} className="flex items-center justify-between gap-3 border-t pt-2 text-sm">
              <span className="min-w-0">{rowColumnName(label, data.accounts)}</span>
              <Cell kind={kind} />
            </li>
          ))}
        </ul>
      )}
    </li>
  );
}

/**
 * A different question from the grid, and it leads the page.
 *
 * The grid says plex.tv is STORING a rule. This says a run looked through a real account's eyes and
 * saw whether Plex ACTS on it. `measured` is what keeps the two apart: an empty result on a run that
 * never looked is "nobody checked", and rendering that as "all clear" is exactly how a live alert
 * gets cleared. The Home-only limit is stated once, here, at reading size.
 */
function EnforcementPanel({ data }: { data: PrivacyStatus }) {
  const { measured, run_id, measured_at, not_enforced } = data.enforcement;
  const exposed = Object.keys(not_enforced);
  const navigate = useNavigate();
  const startRun = useStartRun();
  const ignored = measured && exposed.length > 0;
  // A stored-but-missing rule is an exposure the run's measurement never speaks to; the dot must not
  // read green above it.
  const accountsExposed = EXPOSED_SUMMARIES.includes(data.summary);

  return (
    <Card aria-labelledby="privacy-enforcement-title" role="region">
      <div className="flex flex-wrap items-center justify-between gap-4 px-4 py-4 md:px-6">
        <div className="flex min-w-0 flex-1 basis-80 items-start gap-3">
          <span
            aria-hidden="true"
            className={cn(
              "mt-1.5 size-2.5 shrink-0 rounded-full",
              !measured ? "bg-warning" : ignored ? "bg-destructive" : accountsExposed ? "bg-warning" : "bg-success",
            )}
          />
          <div className="min-w-0 space-y-1">
            <h2
              id="privacy-enforcement-title"
              className={cn(
                "font-semibold",
                !measured ? "text-warning" : ignored ? "text-destructive-text" : "text-foreground",
              )}
            >
              {!measured
                ? "Plex hasn’t been checked applying these rules recently"
                : ignored
                  ? "Plex is ignoring the privacy filter"
                  : "Plex was applying the rules when last checked"}
            </h2>
            {!measured && (
              <p className="text-sm text-muted-foreground">
                The last few runs didn&rsquo;t get as far as looking, so nothing here says whether Plex is
                applying the rules. Every run tries again, so the next one may answer it.
              </p>
            )}
            {measured && !ignored && (
              <p className="text-sm text-muted-foreground">
                Checked in run #{run_id}
                {measured_at ? ` on ${new Date(measured_at).toLocaleString()}` : ""}: Plex was applying the
                hide rules on the accounts checked.
              </p>
            )}
            {ignored && (
              <p className="text-sm text-destructive-text" role="alert">
                {exposed.join(", ")} can see rows belonging to other people even though Plex saved the hide
                rules (run #{run_id}). Shortlist checks one account of each kind, so this is likely every
                shared or managed account, not only the {exposed.length === 1 ? "one" : "ones"} named.
                Please open an issue &mdash; there is no setting here that fixes it.
              </p>
            )}
            <p className="text-sm text-muted-foreground">
              Verify now starts a run of every row. Partway through, it looks at Home as one shared and one
              managed account. Home screen only &mdash; the Collections tab and Related shelves
              can&rsquo;t be checked.
            </p>
          </div>
        </div>
        <Button
          loading={startRun.isPending}
          onClick={() => startRun.mutate({}, { onSuccess: (created) => void navigate(`/runs/${created.run_id}`) })}
        >
          {!startRun.isPending && <ScanEye aria-hidden="true" />}
          Verify now
        </Button>
      </div>
      {startRun.isError && (
        <div className="px-4 pb-4 md:px-6">
          <MutationAlert error={startRun.error} fallback="Couldn’t start a run. Check the server log and try again." />
        </div>
      )}
    </Card>
  );
}
