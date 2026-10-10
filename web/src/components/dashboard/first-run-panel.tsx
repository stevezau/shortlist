import { FlaskConical, Play } from "lucide-react";
import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { UserAvatar } from "@/components/user-avatar";
import { nameList } from "@/lib/run-privacy";
import type { User } from "@/lib/types";
import { dayTime } from "@/lib/when";
import { personName } from "@/lib/user-names";

/** Faces shown before the rest are summed up in words. */
const FACES = 6;

/**
 * The dashboard before anything has run: one panel that does the one thing worth doing.
 *
 * Both choices start the same run the Runs page does. No link off to Runs: this is the first thing a
 * new owner sees, and sending them to an empty list to find a button is the detour this panel exists
 * to remove.
 */
export function FirstRunPanel({
  people,
  nextRun,
  pending,
  onRun,
}: {
  /** Everyone switched on to get a row. */
  people: User[];
  /** When the schedule would do this anyway, if it is set to. */
  nextRun: string | undefined;
  /** Which of the two is in flight, if either. */
  pending: "run" | "dry" | null;
  onRun: (dryRun: boolean) => void;
}) {
  const names = people.map((person) => personName(person));
  return (
    <section
      aria-labelledby="first-run-heading"
      className="grid gap-4 rounded-lg border bg-card p-5 shadow-elevated sm:p-7"
    >
      <div>
        <h2 id="first-run-heading" className="text-xl font-semibold tracking-tight">
          Build everyone’s rows for the first time
        </h2>
        {people.length > 0 ? (
          <p className="mt-1.5 max-w-prose text-[15px] text-muted-foreground">
            Reads {people.length === 1 ? "1 person’s" : `${people.length} people’s`} watch history,
            builds a private row for each, and hides every row from everyone else before it appears.
          </p>
        ) : (
          <p className="mt-1.5 max-w-prose text-[15px] text-muted-foreground">
            Nobody is switched on to get a row yet.{" "}
            <Link
              to="/users"
              className="rounded-sm text-accent-foreground underline underline-offset-2 hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
            >
              Switch people on in Users
            </Link>
            , then build their rows here.
          </p>
        )}
      </div>

      {people.length > 0 && (
        <div className="flex flex-wrap items-center gap-2.5 text-sm text-muted-foreground">
          <span className="flex -space-x-1.5" aria-hidden="true">
            {names.slice(0, FACES).map((name) => (
              <UserAvatar key={name} name={name} size="sm" className="ring-2 ring-card" />
            ))}
          </span>
          <span className="min-w-0 [overflow-wrap:anywhere]">{nameList(names, FACES)}</span>
        </div>
      )}

      <div className="flex flex-col gap-5 pt-1 sm:flex-row sm:flex-wrap sm:gap-7">
        <div className="grid justify-items-start gap-2">
          <Button onClick={() => onRun(false)} loading={pending === "run"} disabled={pending !== null}>
            {pending !== "run" && <Play aria-hidden="true" />}
            Run now
          </Button>
          <span className="max-w-[34ch] text-xs text-muted-foreground">
            Takes a few minutes. It opens the run so you can watch, or leave it running.
          </span>
        </div>
        <div className="grid justify-items-start gap-2">
          <Button
            variant="outline"
            onClick={() => onRun(true)}
            loading={pending === "dry"}
            disabled={pending !== null}
          >
            {pending !== "dry" && <FlaskConical aria-hidden="true" />}
            Dry run first
          </Button>
          <span className="max-w-[34ch] text-xs text-muted-foreground">
            Shows the exact changes without touching Plex.
          </span>
        </div>
      </div>

      <p className="border-t pt-3.5 text-[13px] text-faint-foreground">
        {nextRun
          ? `Or leave it: the scheduled run (${dayTime(nextRun)}) does the same thing, and this page fills in with how it went.`
          : "No row has a schedule yet, so rows build only when you run them."}
      </p>
    </section>
  );
}
