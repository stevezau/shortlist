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
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { settingBool } from "@/lib/format";
import { usePrivacyStatus, useSaveSettings, useSettings, useStartRun } from "@/lib/queries";
import type { AccountPrivacy, PrivacyStatus } from "@/lib/types";
import { USER_TYPE_LABEL } from "@/lib/user-profile";
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
        subtitle="Which rows each Plex account can see, read live from plex.tv."
        actions={
          <Button variant="outline" onClick={() => void query.refetch()} loading={query.isFetching}>
            {!query.isFetching && <RefreshCw aria-hidden="true" />}
            Read again
          </Button>
        }
      />
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
            <StatusStrip data={data} />
            <Summary data={data} onRetry={() => void query.refetch()} />
            <AccountsTable accounts={data.accounts} />
          </div>
        )}
      </QueryBoundary>
      {/* Outside the live read on purpose: a server-wide setting must stay reachable when plex.tv
          is down, which is exactly when an owner comes here. */}
      <PolicyPanel />
      {query.data && query.data.accounts.length > 0 && <EnforcementPanel data={query.data} />}
    </div>
  );
}

/** "sarah, mike and jess" — or "sarah, mike and 3 more" past three names. */
function nameList(names: string[]): string {
  if (names.length <= 3) return names.length < 2 ? (names[0] ?? "") : `${names.slice(0, -1).join(", ")} and ${names.at(-1)}`;
  return `${names.slice(0, 2).join(", ")} and ${names.length - 2} more`;
}

type Tone = "ok" | "warn" | "bad" | "neutral";

const DOT: Record<Tone, string> = {
  ok: "bg-success",
  warn: "bg-warning",
  bad: "bg-destructive",
  neutral: "",
};

function StripCell({ label, tone, value, sub }: { label: string; tone: Tone; value: ReactNode; sub: ReactNode }) {
  return (
    <div className="min-w-0 space-y-1 bg-card px-4 py-3 sm:px-5">
      <p className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
        {tone !== "neutral" && <span aria-hidden="true" className={cn("size-1.5 shrink-0 rounded-full", DOT[tone])} />}
        {label}
      </p>
      <div className="text-lg font-semibold tabular-nums leading-7">{value}</div>
      <p className="text-xs text-muted-foreground">{sub}</p>
    </div>
  );
}

/** States in which an account can see rows that are not its own. A choice to leave an account alone
 *  is not one of them — that account hides nothing, but by the owner's own decision. */
const EXPOSED_STATES = ["missing", "unreadable_filter", "refused_by_plex", "unknown"];
const UNHIDEABLE_REASON: Record<string, string> = {
  refused_by_plex: "Restriction Profile",
  unreadable_filter: "“&” in a label",
};

/**
 * Four readings at a glance, each from the same live read as the rest of the page and never more
 * confident than it: a failed read is "Unknown", an empty server says so instead of "0 of 0", and an
 * unmeasured enforcement check is a warning, because nothing says the rules are being applied. The
 * snapshot count is the exception that is not a live read: it is Shortlist's own record of each
 * account's filters before it changed them, so it stays true when plex.tv cannot be read.
 */
function StatusStrip({ data }: { data: PrivacyStatus }) {
  const unreadable = data.summary === "unreadable" || data.summary === "rows_unknown";
  // The owner has no share to filter (Plex), so they are not in the count either way.
  const filterable = data.accounts.filter((account) => account.state !== "owner");
  const hiding = filterable.filter((account) => account.state === "hiding");
  const unhideable = filterable.filter((account) => account.state in UNHIDEABLE_REASON);
  const { measured, measured_at, run_id, not_enforced } = data.enforcement;
  const ignored = Object.keys(not_enforced).length > 0;

  const hidingCell = unreadable
    ? { tone: "warn" as Tone, value: "Unknown", sub: "Couldn’t read every filter from Plex" }
    : filterable.length === 0
      ? { tone: "neutral" as Tone, value: "—", sub: "No shared or managed accounts" }
      : {
          tone: (hiding.length === filterable.length
            ? "ok"
            : filterable.some((account) => EXPOSED_STATES.includes(account.state))
              ? "warn"
              : "neutral") as Tone,
          value: `${hiding.length} of ${filterable.length}`,
          sub: hiding.length ? nameList(hiding.map((account) => account.display_name)) : "None yet",
        };

  return (
    <section aria-label="Privacy status" className="grid gap-px overflow-hidden rounded-lg border bg-border sm:grid-cols-2 lg:grid-cols-4">
      <StripCell label="Accounts hiding every row" {...hidingCell} />
      <StripCell
        label="Accounts that cannot be hidden"
        tone={unreadable ? "warn" : unhideable.length ? "warn" : "neutral"}
        value={unreadable ? "Unknown" : unhideable.length}
        sub={
          unreadable
            ? "Read again once plex.tv answers"
            : unhideable.length
              ? nameList(unhideable.map((account) => `${account.display_name} · ${UNHIDEABLE_REASON[account.state]}`))
              : "None"
        }
      />
      <StripCell
        label="Last verified"
        tone={!measured ? "warn" : ignored ? "bad" : "ok"}
        value={
          !measured ? (
            <Badge variant="warning" className="font-medium">Not checked recently</Badge>
          ) : measured_at ? (
            new Date(measured_at).toLocaleDateString()
          ) : (
            "Checked"
          )
        }
        sub={!measured ? "The last few runs didn’t get that far" : ignored ? `Run #${run_id}: Plex ignored the rules` : `Run #${run_id}`}
      />
      <StripCell label="Snapshots kept" tone="neutral" value={data.snapshots_kept} sub="Restored on uninstall" />
    </section>
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
    // The same Card, title and type sizes as Accounts and "Is Plex applying the rules?" beside it: a
    // panel drawn a size smaller than its neighbours reads as less important, and this one is not.
    <Card aria-labelledby="privacy-policy-title" role="region">
      <CardHeader>
        <CardTitle id="privacy-policy-title" role="heading" aria-level={2}>
          Policy
        </CardTitle>
        <CardDescription>
          Server-wide rules for who sees what. A switch saves as you flip it, and applies on the next run.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <div className="divide-y">
          <div className="space-y-2 pb-4">
            <div className="flex items-start justify-between gap-5">
              <div className="min-w-0 space-y-1">
                <p className="font-medium">Disabled users see nothing</p>
                <p className="max-w-prose text-sm text-muted-foreground">
                  When you disable a user, hide every shared row from them too, even the public
                  &ldquo;Popular on this server&rdquo; rows everyone else sees.
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
            <p className="flex items-start gap-1.5 text-sm text-foreground">
              <EyeOff className="mt-0.5 h-3.5 w-3.5 shrink-0 text-accent-foreground" aria-hidden="true" />
              On: a disabled account sees no Shortlist row at all. Off: it still sees public shared rows
              like any other account with library access.
            </p>
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
          <div className="flex items-start justify-between gap-5 pt-4">
            <div className="min-w-0 space-y-1">
              <p className="font-medium">Your own account sees every row</p>
              <p className="max-w-prose text-sm text-muted-foreground">
                Plex cannot filter its own account — the one that owns the server — so your Home shows
                everyone&rsquo;s rows. What you see is not what they see.
              </p>
            </div>
            <Badge className="shrink-0 font-medium">Plex limit</Badge>
          </div>
        </div>
      </CardContent>
    </Card>
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
  if (data.summary === "not_enforced") {
    // Only reached when NO account is missing a rule — the server ranks a missing rule higher,
    // because that one the next run fixes. So here the filters really are complete and the fault is
    // Plex's, which is what makes the "nothing below is wrong" claim safe to make.
    const who = Object.keys(data.enforcement.not_enforced);
    return (
      <Banner tone="bad" role="alert">
        <p>
          <strong>
            Plex saved every hide rule and is showing other people's rows
            anyway.
          </strong>{" "}
          Checked through the eyes of {who.join(", ")} in run #
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
  if (data.summary === "filter_unreadable") {
    // Not folded into "missing": that banner promises the next run fixes it, and for these accounts
    // no run can — Plex fails on a label with a raw `&` in it until the owner renames it.
    const stuck = data.accounts.filter((a) => a.state === "unreadable_filter");
    return (
      <Banner tone="bad" role="alert">
        <p>
          <strong>
            {stuck.length === 1
              ? `Plex can't read the restrictions on ${stuck[0]?.display_name}, so Shortlist can't hide rows from them.`
              : `Plex can't read the restrictions on ${stuck.length} accounts, so Shortlist can't hide rows from them.`}
          </strong>{" "}
          A label in those restrictions has an &ldquo;&amp;&rdquo; in its name,
          and Plex returns an error for their Home screen. Rename the label in Plex — for example with
          &ldquo;and&rdquo; — and the next run hides the rows.
        </p>
      </Banner>
    );
  }
  if (data.summary === "unhideable") {
    // Without this branch the sixth verdict fell through to "Shortlist couldn't interpret this
    // reading", printed above an all-clear enforcement panel — so the one thing the escalation
    // exists to surface appeared nowhere on the page.
    const blocked = Object.keys(data.enforcement?.unhideable ?? {});
    // One paragraph, the verdict first and in bold: who, and that a run SAW it. The remedy and its
    // cost follow in plain weight, so the bold sentence is the one a skimming eye takes away.
    return (
      <Banner tone="bad">
        <p>
          <strong className="font-semibold">
            Plex will not hide other people&rsquo;s rows from{" "}
            {blocked.length === 1 ? blocked[0] : `${blocked.length} accounts`} at all, and a run has
            confirmed they can see them.
          </strong>{" "}
          Accounts with a Restriction Profile in Plex reject hide rules outright. Set that
          account&rsquo;s Restriction Profile to <strong>None</strong> in Plex only if that matches the
          account&rsquo;s parental-control needs. This changes Plex&rsquo;s age restrictions; Shortlist
          never changes that profile for you. The next run can then hide their view.{" "}
          <ReadAt at={data.read_at} />
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
  if (data.summary === "clean") {
    // "EVERY account" is only true when every account is actually hiding. An account you chose to
    // leave alone (`manage_sharing = 0`) is not a fault — the verdict deliberately stays clean — but
    // it does not hide anyone's rows, so it cannot be counted in a universal claim. Saying "every"
    // over the top of it is the same over-claim this whole page exists to stop making.
    // Every account that hides NOTHING, not just the ones you chose to leave alone. A
    // parental-profile account has `manage_sharing = 1`, so it IS an account Shortlist manages — it
    // simply cannot be given the rule, because plex.tv rejects the write. Counting it in "every
    // account hides…" is the same over-claim, and `_account_out` ranks `refused_by_plex` above
    // `missing`, so the server-side summary never sees it either.
    const excluded = data.accounts.filter(
      (account) =>
        account.state === "left_alone" || account.state === "refused_by_plex",
    );
    return (
      <Banner tone="good">
        <p>
          {excluded.length > 0 ? (
            <>
              {/* No count. "all N rows" read off `rows_on_plex` counted the account's OWN row too,
                  so the banner claimed "hides all 40" while every line in the table below read
                  "Hides 39 of 39". */}
              Every other account hides the rows that aren&rsquo;t theirs.{" "}
              {excluded.length === 1
                ? `${excluded[0]?.display_name} is excluded from that: ${
                    excluded[0]?.state === "left_alone"
                      ? "you chose to leave their Plex sharing alone."
                      : "Plex refuses hide rules for accounts with a restriction profile."
                  }`
                : `${excluded.length} accounts are excluded from that — see the table below for which and why.`}{" "}
            </>
          ) : (
            <>Every account hides the rows that aren&rsquo;t theirs. </>
          )}
          <ReadAt at={data.read_at} />
        </p>
      </Banner>
    );
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

/** What each state means, in the owner's terms. The evidence is in the sentence, never a bare tick. */
const STATE_COPY: Record<string, { label: string; tone: string }> = {
  hiding: {
    label: "Hiding every row — read from plex.tv just now",
    tone: "text-success",
  },
  missing: { label: "Missing hide rules", tone: "text-destructive-text" },
  unreadable_filter: {
    label: "Plex can't read this account's restrictions",
    tone: "text-destructive-text",
  },
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
        <div className="min-w-0">
          <ul className="min-w-0 divide-y">
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
      <div className="flex min-w-0 flex-1 items-start gap-3 [overflow-wrap:anywhere]">
        <UserAvatar name={account.display_name} />
        <div className="min-w-0">
          <p className="flex flex-wrap items-baseline gap-x-2 break-words font-medium">
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
            {/* Shared or Managed decides what Plex lets an owner restrict, so it sits by the name.
                Not for the owner (the state says it), nor for an account Shortlist has not synced,
                whose kind is only a fallback (see the note below). */}
            {account.user_id !== null && account.user_type !== "owner" && USER_TYPE_LABEL[account.user_type] && (
              <span className="text-[13px] font-normal text-muted-foreground">
                {USER_TYPE_LABEL[account.user_type]}
              </span>
            )}
          </p>
          <p className={cn("text-sm", copy?.tone)}>{copy?.label}</p>
          {(account.state === "missing" ||
            account.state === "unreadable_filter") && (
            <p className="mt-1 text-sm text-muted-foreground">
              Can see: {account.missing.join(", ")}
            </p>
          )}
          {account.state === "unreadable_filter" && (
            <p className="mt-1 text-sm text-muted-foreground">
              A label in their restrictions has an &ldquo;&amp;&rdquo; in its
              name. Rename it in Plex and the next run can hide their view.
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
      {/* A number, not a tick: "12 of 12" and "0 of 0" must not look alike. With its NOUN — "Hides
          2 of 2" left the reader to guess two of what, on the page where the thing being counted
          is the whole point. */}
      <p className="shrink-0 whitespace-nowrap pl-12 text-sm tabular-nums text-muted-foreground sm:pl-0 sm:pt-1">
        {account.state === "owner" || account.state === "unknown"
          ? "—"
          : `Hides ${account.hides.length} of ${account.should_hide.length} ${
              account.should_hide.length === 1 ? "row" : "rows"
            }`}
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
  const navigate = useNavigate();
  const startRun = useStartRun();

  return (
    <Card>
      <CardHeader>
        <CardTitle>Is Plex applying the rules?</CardTitle>
        <CardDescription>
          Storing a hide rule and acting on it are different things, so each run
          reads one account of each kind as that person.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-4 text-sm">
        {/* Staleness with something to DO about it. This used to report "not checked recently" and
            stop — a status with no next step, on the panel an owner opens when they are already
            worried. The check rides a RUN (it looks through one real account's eyes per kind), so
            the honest action is to start one. */}
        {!measured && (
          <div className="flex flex-col gap-2 sm:flex-row sm:items-start sm:gap-3">
            <Badge variant="warning" className="w-fit shrink-0 font-medium">Not checked recently</Badge>
            <p className="text-muted-foreground">
              The last few runs didn&rsquo;t get as far as looking, so nothing
              here says whether Plex is applying the rules. Every run tries
              again, so the next one may answer it.
            </p>
          </div>
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
        <div className="flex flex-col gap-2 sm:flex-row sm:items-center sm:gap-3">
          <Button
            className="w-fit"
            loading={startRun.isPending}
            onClick={() =>
              startRun.mutate({}, { onSuccess: (created) => void navigate(`/runs/${created.run_id}`) })
            }
          >
            {!startRun.isPending && <ScanEye aria-hidden="true" />}
            Verify now
          </Button>
          {/* Said before the click: this is a full run, the same as "Run all rows now", with the
              check partway through — not a read-only probe. */}
          <p className="text-xs text-muted-foreground">
            Starts a run of every row now. Partway through, it looks at Home as
            one shared and one managed account.
          </p>
        </div>
        {startRun.isError && (
          <MutationAlert
            error={startRun.error}
            fallback="Couldn’t start a run. Check the server log and try again."
          />
        )}
        <p className="text-xs text-muted-foreground">
          These checks cover the Home screen. Shortlist has no way to confirm
          what Plex does on the Collections tab or in Related shelves.
        </p>
      </CardContent>
    </Card>
  );
}
