/**
 * "Have an issue?" — one door for everything that goes wrong.
 *
 * The person operating this page is not the person who needs the answer. A maintainer debugging
 * someone else's server only ever sees what that person pastes into a chat window, and everything
 * here is shaped by that:
 *
 *   - the page opens on the PROBLEM, not on a list of nineteen inspectors;
 *   - each check states its verdict in a sentence, because the operator cannot read a table and the
 *     maintainer is not there to read it for them;
 *   - the exact text that will be sent is shown on screen, so "copy" holds no surprises;
 *   - filing the report — the GitHub link and the diagnostics — is the last step of the same page,
 *     not two separate sidebar buttons someone has to know to combine.
 *
 * Every block of copy text is rendered by the SERVER (`text` on each response), never assembled
 * here, so the format is decided and unit-tested in one place.
 */

import { useEffect, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  AlertTriangle,
  Bug,
  Check,
  ChevronDown,
  ClipboardCopy,
  Download,
  LifeBuoy,
  Search,
  X,
} from "lucide-react";

import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api";
import { useVersion } from "@/lib/queries";
import { newBugReportUrl } from "@/lib/support";
import { useCopy } from "@/lib/use-copy";
import type { SupportHealth, SupportStatus } from "@/lib/types";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// The catalogue.
//
// One entry per check. `needs` says what the operator must supply first, which is the only thing
// that differs between them — everything else is the same fetch/verdict/copy shape, so the page has
// one panel component rather than nineteen.
// ---------------------------------------------------------------------------

type Needs = "nothing" | "title" | "person" | "person+title" | "read-as";

interface CheckArgs {
  title: string;
  person: string;
  endpoint: string;
  section: string;
}

interface Check {
  id: string;
  label: string;
  blurb: string;
  needs: Needs;
  run: (args: CheckArgs) => Promise<unknown>;
}

const CHECKS: Check[] = [
  {
    id: "title",
    label: "Is this title counted as watched?",
    blurb:
      "Where one title stands for every person: watched record, episodes, cap, delivery.",
    needs: "title",
    run: ({ title }) => api.supportTitle(title),
  },
  {
    id: "person",
    label: "What have we read of one person's history?",
    blurb:
      "Library by library — separates “watches nothing” from “a library refused their token”.",
    needs: "person",
    run: ({ person }) => api.supportPerson(person),
  },
  {
    id: "rows",
    label: "Which setting actually applied?",
    blurb:
      "The global default versus a per-row override, with the winner marked.",
    needs: "nothing",
    run: () => api.supportRows(),
  },
  {
    id: "row-schedule",
    label: "When does each row next rebuild?",
    blurb:
      "A setting change does nothing until the row rebuilds. This says when that is.",
    needs: "nothing",
    run: () => api.supportRowSchedule(),
  },
  {
    id: "libraries",
    label: "Which libraries can Shortlist see?",
    blurb: "Key, kind, and how many titles Plex matched to a TMDB id.",
    needs: "nothing",
    run: () => api.supportLibraries(),
  },
  {
    id: "connection",
    label: "Can we read everyone's history?",
    blurb:
      "Per person: do we hold a working token, and which libraries has it ever read.",
    needs: "nothing",
    run: () => api.supportConnection(),
  },
  {
    id: "read-as",
    label: "Ask Plex directly, as one person",
    blurb:
      "Reads the server using their own token — the one thing you cannot do from a browser.",
    needs: "read-as",
    run: ({ person, endpoint, section }) =>
      api.supportReadAs(person, endpoint, section),
  },
  {
    id: "sharing",
    label: "Who can see whose rows?",
    blurb:
      "The live share filters on Plex, with our privacy exclusions separated out.",
    needs: "nothing",
    run: () => api.supportSharing(),
  },
  {
    id: "drift",
    label: "Does Plex match our records?",
    blurb:
      "Rows we think we delivered, against what is actually on the server.",
    needs: "nothing",
    run: () => api.supportDrift(),
  },
  {
    id: "pick",
    label: "Why is this in their row?",
    blurb:
      "The seed it came from, the source that suggested it, and how strongly.",
    needs: "person+title",
    run: ({ person, title }) => api.supportPick(person, title),
  },
  {
    id: "missing",
    label: "Why is this NOT in their row?",
    blurb:
      "Never suggested, or suggested and then dropped — and by which stage.",
    needs: "person+title",
    run: ({ person, title }) => api.supportMissing(person, title),
  },
  {
    id: "funnel",
    label: "Why is their row short?",
    blurb:
      "Counts at every stage, so the stage that emptied the row names itself.",
    needs: "person",
    run: ({ person }) => api.supportFunnel(person),
  },
  {
    id: "ai",
    label: "What did the AI do?",
    blurb: "Provider, tokens spent per step, and any error it hit.",
    needs: "person",
    run: ({ person }) => api.supportAi(person),
  },
  {
    id: "timeline",
    label: "What has been happening?",
    blurb: "Runs, jobs and changes on one axis, in your local time.",
    needs: "nothing",
    run: () => api.supportTimeline(""),
  },
  {
    id: "settings-history",
    label: "What changed recently?",
    blurb: "Settings edits, and whether a rebuild has happened since.",
    needs: "nothing",
    run: () => api.supportSettingsHistory(),
  },
  {
    id: "errors",
    label: "What errors has it logged?",
    blurb: "Recent warnings and errors, already stripped of anything secret.",
    needs: "nothing",
    run: () => api.supportErrors(),
  },
  {
    id: "runs",
    label: "How did the last few runs go?",
    blurb: "Per run: who it could not build for, and the error it hit.",
    needs: "nothing",
    run: () => api.supportRecentRuns(),
  },
  {
    id: "jobs",
    label: "Is background work stuck?",
    blurb: "Queued, running and failed jobs, with the last error.",
    needs: "nothing",
    run: () => api.supportJobs(),
  },
  {
    id: "clocks",
    label: "Are the clocks right?",
    blurb: "Timezone, offset, and when each scheduled job next runs.",
    needs: "nothing",
    run: () => api.supportClocks(),
  },
  {
    id: "database",
    label: "Is the database healthy?",
    blurb: "Migration version, tables actually present, size.",
    needs: "nothing",
    run: () => api.supportDatabase(),
  },
  {
    id: "config",
    label: "Where did my settings come from?",
    blurb:
      "Environment variables seed the database once, then stop mattering. This says which won.",
    needs: "nothing",
    run: () => api.supportConfig(),
  },
];

/** The common problems, in the words someone would use. Each opens one check. */
const PROBLEMS: { title: string; blurb: string; check: string }[] = [
  {
    title: "They keep seeing something they've watched",
    blurb:
      "Checks whether we have a watched record for it, and which cap applied.",
    check: "title",
  },
  {
    title: "A setting I changed did nothing",
    blurb: "Shows which value applied, and when the row next rebuilds.",
    check: "rows",
  },
  {
    title: "One person's recommendations look wrong",
    blurb: "Shows what we've managed to read of their history.",
    check: "person",
  },
  {
    title: "Someone can see another person's row",
    blurb: "Compares every share filter against the rows that exist.",
    check: "sharing",
  },
  {
    title: "A row is empty or too short",
    blurb: "Walks the funnel and names the stage that emptied it.",
    check: "funnel",
  },
  {
    title: "Something went wrong and I don't know what",
    blurb: "Shows the recent errors, and which people the last runs failed on.",
    check: "errors",
  },
  {
    title: "Rows aren't updating at all",
    blurb: "Checks the queue, the schedule and the clocks.",
    check: "jobs",
  },
];

// ---------------------------------------------------------------------------

export function IssuePage() {
  const queryClient = useQueryClient();
  // WHERE it was opened from, not just which check. The panel renders directly below the list that
  // was clicked: opening one from the full list used to render it above that list, off-screen
  // upward, so the click looked like it had done nothing at all.
  const [openFrom, setOpenFrom] = useState<"problems" | "all" | null>(null);
  const [openId, setOpenId] = useState<string | null>(null);
  const [showAll, setShowAll] = useState(false);

  const pick = (id: string, from: "problems" | "all") => {
    const same = openId === id;
    setOpenId(same ? null : id);
    setOpenFrom(same ? null : from);
  };

  const status = useQuery({
    queryKey: ["support", "status"],
    queryFn: api.supportStatus,
    // The mode expires on its own, so a page left open would otherwise go on claiming the checks
    // are usable long after they started refusing.
    refetchInterval: 60_000,
  });

  const toggle = useMutation({
    mutationFn: (on: boolean) =>
      on ? api.enableSupport() : api.disableSupport(),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ["support"] }),
  });

  const enabled = status.data?.enabled ?? false;
  const open = CHECKS.find((c) => c.id === openId) ?? null;

  return (
    <div className="mx-auto flex max-w-5xl flex-col gap-6 p-6">
      <header className="flex flex-col gap-2">
        <h1 className="flex items-center gap-2 text-2xl font-semibold tracking-tight">
          <LifeBuoy className="h-6 w-6 text-primary" aria-hidden="true" />
          Have an issue?
        </h1>
        <p className="max-w-2xl text-sm text-muted-foreground">
          Run a few read-only checks to find out what's happening — most answer
          the question on their own. If they don't, the last step turns what you
          found into a bug report. Nothing here changes your Plex server, your
          rows, or your settings.
        </p>
      </header>

      {status.isLoading ? (
        <div
          role="status"
          aria-label="Checking"
          className="h-24 animate-pulse rounded-lg border border-border bg-card"
        />
      ) : status.isError ? (
        <ErrorNote
          message={apiErrorMessage(
            status.error,
            "Could not reach the Shortlist server.",
          )}
          onRetry={() => void status.refetch()}
        />
      ) : (
        <ModeBanner
          status={status.data}
          busy={toggle.isPending}
          onToggle={(on) => toggle.mutate(on)}
          error={
            toggle.isError
              ? apiErrorMessage(toggle.error, "Could not change that.")
              : null
          }
        />
      )}

      {enabled ? (
        <>
          <HealthStrip />

          <section className="flex flex-col gap-3">
            <h2 className="text-lg font-semibold">What's the problem?</h2>
            <div className="grid gap-3 sm:grid-cols-2">
              {PROBLEMS.map((problem) => (
                <button
                  key={problem.check}
                  type="button"
                  onClick={() => pick(problem.check, "problems")}
                  aria-expanded={openId === problem.check}
                  className={cn(
                    "flex flex-col gap-1 rounded-lg border p-4 text-left transition-colors",
                    openId === problem.check
                      ? "border-primary bg-accent"
                      : "border-border bg-card hover:bg-elevated",
                  )}
                >
                  <span className="text-sm font-semibold">{problem.title}</span>
                  <span className="text-xs text-muted-foreground">
                    {problem.blurb}
                  </span>
                </button>
              ))}
            </div>
          </section>

          {open && openFrom === "problems" ? (
            <CheckPanel key={open.id} check={open} />
          ) : null}

          <section className="flex flex-col gap-3">
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              aria-expanded={showAll}
              className="flex w-fit items-center gap-1.5 text-sm font-medium text-muted-foreground hover:text-foreground"
            >
              <ChevronDown
                className={cn(
                  "h-4 w-4 transition-transform",
                  showAll && "rotate-180",
                )}
                aria-hidden="true"
              />
              {showAll ? "Hide" : "Show"} all {CHECKS.length} checks
            </button>
            {showAll ? (
              <div className="grid gap-2 sm:grid-cols-2">
                {CHECKS.map((check) => (
                  <button
                    key={check.id}
                    type="button"
                    onClick={() => pick(check.id, "all")}
                    aria-expanded={openId === check.id}
                    className={cn(
                      "flex flex-col gap-0.5 rounded-md border p-3 text-left transition-colors",
                      openId === check.id
                        ? "border-primary bg-accent"
                        : "border-border bg-card hover:bg-elevated",
                    )}
                  >
                    <span className="text-sm font-medium">{check.label}</span>
                    <span className="text-xs text-muted-foreground">
                      {check.blurb}
                    </span>
                  </button>
                ))}
              </div>
            ) : null}
          </section>

          {/* The second slot. A check opened from the full list renders HERE, below that list —
              rendering it in the slot above meant the click scrolled nothing into view and looked
              like it had failed. */}
          {open && openFrom === "all" ? (
            <CheckPanel key={open.id} check={open} />
          ) : null}

          <ReportSection />
        </>
      ) : null}
    </div>
  );
}

function ModeBanner({
  status,
  busy,
  onToggle,
  error,
}: {
  status: SupportStatus | undefined;
  busy: boolean;
  onToggle: (on: boolean) => void;
  error: string | null;
}) {
  const enabled = status?.enabled ?? false;
  const seconds = status?.seconds_remaining ?? 0;
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);

  return (
    <section
      className={cn(
        "flex flex-col gap-3 rounded-lg border p-4",
        enabled ? "border-primary/40 bg-accent" : "border-border bg-card",
      )}
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div className="flex flex-col gap-1">
          <p className="text-sm font-semibold">
            {enabled ? "Checks are switched on" : "Switch on the checks"}
          </p>
          <p className="text-xs text-muted-foreground">
            {enabled
              ? `They switch themselves off in ${hours}h ${minutes}m. You can switch them off sooner.`
              : "They stay off until you ask for them, and switch themselves off again after 24 hours."}
          </p>
        </div>
        <Button
          onClick={() => onToggle(!enabled)}
          disabled={busy}
          variant={enabled ? "outline" : "default"}
        >
          {enabled ? "Switch off" : "Switch on the checks"}
        </Button>
      </div>
      {error ? <p className="text-sm text-destructive-text">{error}</p> : null}
    </section>
  );
}

function CopyButton({
  text,
  label = "Copy for support",
}: {
  text: string;
  label?: string;
}) {
  const { state, copy } = useCopy(2500);
  return (
    <Button size="sm" variant="outline" onClick={() => copy(text)}>
      {state === "copied" ? (
        <Check className="mr-2 h-4 w-4" aria-hidden="true" />
      ) : (
        <ClipboardCopy className="mr-2 h-4 w-4" aria-hidden="true" />
      )}
      {state === "copied"
        ? "Copied"
        : state === "error"
          ? "Select the text and press Ctrl+C"
          : label}
    </Button>
  );
}

function ErrorNote({
  message,
  onRetry,
}: {
  message: string;
  onRetry?: () => void;
}) {
  return (
    <div className="flex flex-wrap items-center gap-3 rounded-lg border border-destructive/40 bg-card p-4">
      <p className="text-sm text-destructive-text">{message}</p>
      {onRetry ? (
        <Button size="sm" variant="outline" onClick={onRetry}>
          Try again
        </Button>
      ) : null}
    </div>
  );
}

function Verdict({
  bad,
  children,
}: {
  bad: boolean;
  children: React.ReactNode;
}) {
  return (
    <p
      className={cn(
        "flex items-start gap-2 rounded-md p-3 text-sm",
        bad
          ? "bg-destructive/10 text-destructive-text"
          : "bg-success/10 text-foreground",
      )}
    >
      {bad ? (
        <AlertTriangle
          className="mt-0.5 h-4 w-4 flex-none"
          aria-hidden="true"
        />
      ) : (
        <Check className="mt-0.5 h-4 w-4 flex-none" aria-hidden="true" />
      )}
      <span>{children}</span>
    </p>
  );
}

/**
 * The verdict sentence for a result, derived from the fields the server already flags.
 *
 * Every check reports its finding as DATA (`flagged`, `never_read`, `problems`, …) rather than
 * leaving the page to re-derive it from a table — so this stays a lookup, and the page and the copy
 * text can never disagree about whether something is wrong.
 */
function verdictFor(
  id: string,
  data: Record<string, unknown>,
): { bad: boolean; text: string } | null {
  const list = (key: string): string[] =>
    Array.isArray(data[key]) ? (data[key] as string[]) : [];
  const count = (key: string): number =>
    Array.isArray(data[key]) ? (data[key] as unknown[]).length : 0;

  switch (id) {
    case "title": {
      // `flagged_detail` names the TITLE as well as the person. A search is a substring match, so it
      // can span a whole franchise — "was given this title" would be ambiguous about which.
      const detail = list("flagged_detail");
      const capped = data.capped
        ? " Only the first matches were read, so narrow the search to be sure."
        : "";
      return detail.length
        ? {
            bad: true,
            text: `Given even though the row is set to show nothing they've watched: ${detail.join(", ")}.${capped}`,
          }
        : {
            bad: false,
            text: `Nothing unexpected — everyone who was given a matching title is allowed to see it.${capped}`,
          };
    }
    case "person": {
      const never = list("never_read");
      return never.length
        ? {
            bad: true,
            text: `We have never been able to read ${never.length === 1 ? "one library" : `${never.length} libraries`} for this person, so anything they watched there is invisible to Shortlist.`,
          }
        : { bad: false, text: "We can read every library for this person." };
    }
    case "connection": {
      const problems = list("problems");
      return problems.length
        ? { bad: true, text: `We cannot fully read: ${problems.join(", ")}.` }
        : { bad: false, text: "Everyone's history can be read." };
    }
    case "drift": {
      if (data.error) return { bad: true, text: String(data.error) };
      const missing = count("missing_on_plex");
      const orphans = count("orphans_on_plex");
      return missing || orphans
        ? {
            bad: true,
            text: `${missing} row(s) missing from Plex, ${orphans} unexpected collection(s) on it.`,
          }
        : {
            bad: false,
            text: "Everything we recorded exists on the server, and nothing extra.",
          };
    }
    case "settings-history":
      return data.change_after_last_build
        ? {
            bad: true,
            text: "A setting changed after the last build, so it has not taken effect yet.",
          }
        : {
            bad: false,
            text: "Every setting change has been applied by a build since.",
          };
    case "jobs":
      return Number(data.failed ?? 0) > 0
        ? {
            bad: true,
            text: `${String(data.failed)} background job(s) failed.`,
          }
        : { bad: false, text: "No failed background work." };
    case "database": {
      const missing = list("missing_tables");
      return missing.length
        ? { bad: true, text: `The database is missing: ${missing.join(", ")}.` }
        : {
            bad: false,
            text: `Schema is at ${String(data.head)} and complete.`,
          };
    }
    case "missing":
      return data.verdict ? { bad: true, text: String(data.verdict) } : null;
    case "sharing": {
      if (data.error) return { bad: true, text: String(data.error) };
      // The highest-stakes verdict on the page: someone can see a row that is not theirs.
      const leaking = list("missing_excludes_for");
      return leaking.length
        ? {
            bad: true,
            text: `${leaking.join(", ")} can see at least one row that isn't theirs. Run Shortlist again — every run re-merges the exclusions — then check here again.`,
          }
        : {
            bad: false,
            text: "Every row is hidden from everyone it doesn't belong to.",
          };
    }
    case "libraries":
      return data.error ? { bad: true, text: String(data.error) } : null;
    default:
      return null;
  }
}

/**
 * One check: collect what it needs, run it, show the verdict, then show the exact text that will be
 * copied. Showing the text rather than re-rendering it as a table is deliberate — what is on screen
 * is precisely what gets sent, so there is nothing to be surprised by after pasting.
 */
function CheckPanel({ check }: { check: Check }) {
  const panel = useRef<HTMLElement | null>(null);
  const [title, setTitle] = useState("");
  const [person, setPerson] = useState("");
  const [endpoint, setEndpoint] = useState("libraries");
  const [section, setSection] = useState("");

  // Belt to the two-slot braces: on a short window the panel can still open below the fold, and a
  // check that needs typing is useless if its input is off-screen. `block: "nearest"` scrolls only
  // when it actually has to, so an already-visible panel doesn't jump under the cursor. Keyed on
  // `check.id`, so switching checks re-runs it.
  useEffect(() => {
    // Deferred a frame. Called straight from the effect it did nothing at all (verified in a real
    // browser: the panel stayed 1688px down the page with scrollY still 0) — the panel has only just
    // been committed, and `block: "nearest"` on a not-yet-laid-out box decides no scroll is needed.
    // One frame later the geometry is real.
    //
    // `matchMedia` and `scrollIntoView` are both feature-detected rather than assumed: neither exists
    // in jsdom, and scrolling is a convenience — nothing about the check depends on it.
    const frame = requestAnimationFrame(() => {
      const reduced =
        typeof window.matchMedia === "function" &&
        window.matchMedia("(prefers-reduced-motion: reduce)").matches;
      // `start`, not `nearest`. Nearest scrolls the minimum, which left the panel's input sitting on
      // the very bottom edge of the viewport — technically visible, useless to type into. Start puts
      // the heading and the form together at the top, with `scroll-mt-6` for breathing room.
      panel.current?.scrollIntoView?.({
        block: "start",
        behavior: reduced ? "auto" : "smooth",
      });
    });
    return () => cancelAnimationFrame(frame);
  }, [check.id]);
  const [args, setArgs] = useState<CheckArgs | null>(
    check.needs === "nothing"
      ? { title: "", person: "", endpoint: "", section: "" }
      : null,
  );

  const result = useQuery({
    queryKey: ["support", check.id, args],
    queryFn: () => check.run(args as CheckArgs),
    enabled: args !== null,
    retry: false,
  });

  const data = (result.data ?? null) as Record<string, unknown> | null;
  const verdict = data ? verdictFor(check.id, data) : null;
  const needsTitle = check.needs === "title" || check.needs === "person+title";
  const needsPerson =
    check.needs === "person" ||
    check.needs === "person+title" ||
    check.needs === "read-as";
  const ready =
    (!needsTitle || title.trim().length > 0) &&
    (!needsPerson || person.trim().length > 0) &&
    // The server refuses a watched-* read with no library (400), so don't let the click happen.
    (check.needs !== "read-as" ||
      !endpoint.startsWith("watched-") ||
      section.length > 0);

  // Shared across every panel by react-query's cache, so opening ten checks fetches this once.
  const hints = useQuery({
    queryKey: ["support", "suggestions"],
    queryFn: api.supportSuggestions,
    enabled: needsPerson || needsTitle,
    staleTime: 5 * 60_000,
  });
  const people = hints.data?.people ?? [];
  const titles = hints.data?.titles ?? [];
  // Caught before the request, and only once the roster is actually known — warning on an empty list
  // would flag every name while the fetch was still in flight.
  const unknownPerson =
    people.length > 0 &&
    person.trim().length > 0 &&
    !people.some((u) => u.slug.toLowerCase() === person.trim().toLowerCase());

  // The library picker for "read as a user". Its own query so a broken Plex connection costs this
  // control and not the panel — the field falls back to free text below.
  const libraries = useQuery({
    queryKey: ["support", "libraries"],
    queryFn: api.supportLibraries,
    enabled: check.needs === "read-as",
    staleTime: 5 * 60_000,
  });
  const sections = libraries.data?.libraries ?? [];

  return (
    <section
      ref={panel}
      className="flex scroll-mt-6 flex-col gap-3 rounded-lg border border-border bg-card p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">{check.label}</h2>
        {typeof data?.text === "string" ? (
          <CopyButton text={data.text} />
        ) : null}
      </div>

      {check.needs !== "nothing" ? (
        <form
          className="flex flex-wrap items-end gap-2"
          onSubmit={(event) => {
            event.preventDefault();
            setArgs({
              title: title.trim(),
              person: person.trim(),
              endpoint,
              section,
            });
          }}
        >
          {needsPerson ? (
            <div className="flex min-w-52 flex-1 flex-col gap-1.5">
              <Label htmlFor={`${check.id}-person`}>Plex username</Label>
              {/* A real `<datalist>`, not a bespoke dropdown: typing a couple of characters filters
                  it, the keyboard works, and there is no "did I spell it right" step. A username
                  typed from memory is the commonest way one of these checks returns nothing. */}
              <Input
                id={`${check.id}-person`}
                list={`${check.id}-person-options`}
                value={person}
                autoComplete="off"
                placeholder={
                  people[0] ? `e.g. ${people[0].slug}` : "start typing a name"
                }
                onChange={(event) => setPerson(event.target.value)}
              />
              <datalist id={`${check.id}-person-options`}>
                {people.map((u) => (
                  <option key={u.slug} value={u.slug}>
                    {u.display_name && u.display_name !== u.slug
                      ? `${u.display_name} — ${u.slug}`
                      : u.slug}
                  </option>
                ))}
              </datalist>
              {unknownPerson ? (
                <p className="text-xs text-warning">
                  No one on this server is called “{person.trim()}”. Pick from
                  the list — it's their Plex username, not their display name.
                </p>
              ) : null}
            </div>
          ) : null}
          {needsTitle ? (
            <div className="flex min-w-52 flex-1 flex-col gap-1.5">
              <Label htmlFor={`${check.id}-title`}>Title</Label>
              {/* Suggestions come from what has actually been recommended or watched, so the list is
                  the titles a question could plausibly be about. Still free text — the lookup is a
                  substring match, and a title nobody has touched yet is a legitimate thing to ask. */}
              <Input
                id={`${check.id}-title`}
                list={`${check.id}-title-options`}
                value={title}
                autoComplete="off"
                placeholder="type a few letters"
                onChange={(event) => setTitle(event.target.value)}
              />
              <datalist id={`${check.id}-title-options`}>
                {titles.map((t) => (
                  <option key={t} value={t} />
                ))}
              </datalist>
            </div>
          ) : null}
          {check.needs === "read-as" ? (
            <>
              <div className="flex min-w-40 flex-col gap-1.5">
                <Label htmlFor={`${check.id}-endpoint`}>What to ask</Label>
                <select
                  id={`${check.id}-endpoint`}
                  value={endpoint}
                  onChange={(event) => setEndpoint(event.target.value)}
                  className="h-9 rounded-md border border-input bg-elevated px-3 text-sm"
                >
                  <option value="libraries">Their libraries</option>
                  <option value="watched-movies">Films they've watched</option>
                  <option value="watched-shows">Shows they've watched</option>
                  <option value="home-rows">Their Home screen</option>
                </select>
              </div>
              {endpoint.startsWith("watched-") ? (
                <div className="flex min-w-44 flex-col gap-1.5">
                  <Label htmlFor={`${check.id}-section`}>Library</Label>
                  {/* Named libraries, not a key. Asking for "e.g. 1" required knowing a Plex section
                      key, which nobody does — and the server rejects a wrong one, so the field was a
                      guessing game. Filtered to the right KIND for the question too: asking for
                      watched films in a TV library returns an empty answer that reads like a finding. */}
                  {sections.length > 0 ? (
                    <select
                      id={`${check.id}-section`}
                      value={section}
                      onChange={(event) => setSection(event.target.value)}
                      className="h-9 rounded-md border border-input bg-elevated px-3 text-sm"
                    >
                      <option value="">Choose a library…</option>
                      {sections
                        .filter((lib) =>
                          endpoint === "watched-movies"
                            ? lib.type === "movie"
                            : lib.type === "show",
                        )
                        .map((lib) => (
                          <option key={lib.key} value={lib.key}>
                            {lib.title}
                          </option>
                        ))}
                    </select>
                  ) : (
                    <Input
                      id={`${check.id}-section`}
                      value={section}
                      placeholder="library key, e.g. 1"
                      onChange={(event) => setSection(event.target.value)}
                    />
                  )}
                </div>
              ) : null}
            </>
          ) : null}
          <Button type="submit" disabled={!ready}>
            <Search className="mr-2 h-4 w-4" aria-hidden="true" />
            Check
          </Button>
        </form>
      ) : null}

      {result.isLoading && args !== null ? (
        <div
          role="status"
          aria-label="Checking"
          className="h-20 animate-pulse rounded-md bg-elevated"
        />
      ) : null}
      {result.isError ? (
        <ErrorNote
          message={apiErrorMessage(result.error, "That check could not run.")}
          onRetry={() => void result.refetch()}
        />
      ) : null}
      {verdict ? <Verdict bad={verdict.bad}>{verdict.text}</Verdict> : null}
      {typeof data?.text === "string" ? (
        <>
          <p className="text-xs text-muted-foreground">
            This is exactly what “Copy for support” puts on your clipboard:
          </p>
          <pre className="max-h-96 overflow-auto rounded-md border border-border bg-elevated p-3 font-mono text-xs leading-relaxed">
            {data.text}
          </pre>
        </>
      ) : null}
    </section>
  );
}

function HealthStrip() {
  const health = useQuery<SupportHealth>({
    queryKey: ["support", "health"],
    queryFn: api.supportHealth,
  });

  if (health.isLoading) {
    return (
      <div
        role="status"
        aria-label="Running checks"
        className="h-28 animate-pulse rounded-lg border border-border bg-card"
      />
    );
  }
  if (health.isError) {
    return (
      <ErrorNote
        message={apiErrorMessage(health.error, "Could not run the checks.")}
        onRetry={() => void health.refetch()}
      />
    );
  }

  const checks = health.data?.checks ?? [];
  const bad = checks.filter((c) => !c.ok);

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h2 className="text-sm font-semibold">Health</h2>
        {health.data?.text ? <CopyButton text={health.data.text} /> : null}
      </div>
      <Verdict bad={bad.length > 0}>
        {bad.length
          ? `${bad.length} of ${checks.length} checks need attention: ${bad.map((c) => c.name).join(", ")}.`
          : "Everything Shortlist depends on is working."}
      </Verdict>
      <ul className="grid gap-2 sm:grid-cols-2 lg:grid-cols-4">
        {checks.map((check) => (
          <li
            key={check.name}
            className={cn(
              "flex flex-col gap-0.5 rounded-md border p-2",
              check.ok ? "border-border bg-elevated" : "border-destructive/40",
            )}
          >
            <span className="flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-muted-foreground">
              {check.ok ? (
                <Check className="h-3 w-3 text-success" aria-hidden="true" />
              ) : (
                <X
                  className="h-3 w-3 text-destructive-text"
                  aria-hidden="true"
                />
              )}
              {check.name}
            </span>
            <span className="text-sm">{check.detail}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** The last step: turn what the checks found into a report someone can act on.
 *
 *  The privacy choice here is the point. The report NAMES the people on the server, because a
 *  maintainer cannot act on "someone can't be read" — and a GitHub issue is public. So the default is
 *  to hide names, and sending them is the deliberate option, not the accident. */
function ReportSection() {
  const version = useVersion();
  const { state, copy } = useCopy(2500);
  // Defaults to hiding names: the button beside this one posts to a public tracker, so the safe
  // choice has to be the one you get without thinking about it.
  const [hideNames, setHideNames] = useState(true);

  return (
    <section className="flex flex-col gap-3 rounded-lg border border-border bg-card p-4">
      <h2 className="text-sm font-semibold">Still stuck? Report it</h2>
      <p className="text-sm text-muted-foreground">
        Open an issue, then attach the report below so whoever picks it up
        already has the answers to the first three questions they'd ask.
      </p>

      <label className="flex items-start gap-2.5 rounded-md border border-border bg-elevated p-3">
        <input
          type="checkbox"
          // Explicit: the wrapping label holds a heading AND a paragraph, so the computed name would
          // be the whole block — unhelpful to a screen reader and to anything matching on it.
          aria-label="Hide everyone's names"
          checked={hideNames}
          onChange={(event) => setHideNames(event.target.checked)}
          className="mt-0.5 h-4 w-4 accent-primary"
        />
        <span className="flex flex-col gap-0.5">
          <span className="text-sm font-medium">
            Hide everyone&rsquo;s names
          </span>
          <span className="text-xs text-muted-foreground">
            Replaces each person with “person1”, “person2” and so on — the same
            label every time, in the logs too, so the report still makes sense.
            Leave this on for a public GitHub issue. Turn it off only if
            you&rsquo;re sending the report privately and it&rsquo;s easier to
            talk about real names.
          </span>
        </span>
      </label>

      <div className="flex flex-wrap gap-2">
        <Button asChild>
          <a
            href={newBugReportUrl(version.data?.current_version ?? "")}
            target="_blank"
            rel="noopener noreferrer"
          >
            <Bug className="mr-2 h-4 w-4" aria-hidden="true" />
            Report a bug on GitHub
          </a>
        </Button>
        {/* Both the fetch (a 500 building the report) and the clipboard write can fail — useCopy
            surfaces either as an error state rather than a silently dead button. */}
        <Button
          variant="outline"
          onClick={() => copy(api.getSupportBundle(hideNames))}
        >
          {state === "copied" ? (
            <Check className="mr-2 h-4 w-4" aria-hidden="true" />
          ) : (
            <ClipboardCopy className="mr-2 h-4 w-4" aria-hidden="true" />
          )}
          <span aria-live="polite">
            {state === "copied"
              ? "Copied — paste it into the issue"
              : state === "error"
                ? "Couldn't copy — use the download instead"
                : "Copy the summary"}
          </span>
        </Button>
        <Button asChild variant="outline">
          <a
            href={api.supportReportZipUrl(hideNames)}
            download="shortlist-report.zip"
          >
            <Download className="mr-2 h-4 w-4" aria-hidden="true" />
            Download everything (with logs)
          </a>
        </Button>
      </div>
      {/* Specific about what IS and ISN'T in there. "No passwords or tokens" was true and
          misleading — it sat next to a button that posts publicly, and someone reading it would
          reasonably conclude the whole thing was safe to publish. */}
      <p className="text-xs text-muted-foreground">
        <strong className="font-medium text-foreground">
          No passwords, tokens or API keys
        </strong>{" "}
        — those are stripped from the report and from the logs, and your
        server&rsquo;s address is reduced to{" "}
        <code className="font-mono">http://&lt;host&gt;</code>. It{" "}
        <strong className="font-medium text-foreground">does</strong> include
        your library names, the titles involved, and — unless you leave the box
        above ticked — the Plex usernames of people on your server. On a busy
        server the summary runs to tens of kilobytes, which is more than a chat
        message holds — attach it as a file if the paste gets cut off, and use
        the download when you want the logs as well.
      </p>
    </section>
  );
}
