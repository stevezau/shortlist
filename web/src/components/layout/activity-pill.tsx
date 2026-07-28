import { Loader2 } from "lucide-react";
import { useRef, useState } from "react";
import { NavLink } from "react-router";

import { STAGE_LABELS } from "@/lib/run-stages";
import { useSSE } from "@/lib/sse";
import { cn } from "@/lib/utils";

interface Activity {
  text: string;
  tone: "active" | "ok" | "error";
}

const LINGER_MS = 8_000;

/**
 * Live "what is Shortlist doing right now" pill, pinned in the sidebar on every
 * page — a run at 03:30 or one started from another tab is visible wherever
 * you are, not only if you happen to be watching the Runs page.
 *
 * This is the ONE deliberate exception to the one-EventSource-per-page rule
 * (rules/frontend.md): the shell holds a second, app-wide subscription so
 * activity is never invisible. Terminal states linger briefly, then the pill
 * disappears — no activity means Shortlist is idle.
 */
export function ActivityPill() {
  const [activity, setActivity] = useState<Activity | null>(null);
  const clearTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const show = (next: Activity, linger = false) => {
    if (clearTimer.current) {
      clearTimeout(clearTimer.current);
      clearTimer.current = null;
    }
    setActivity(next);
    if (linger) {
      clearTimer.current = setTimeout(() => setActivity(null), LINGER_MS);
    }
  };

  useSSE({
    onRunUserStage: (event) =>
      show({
        text: `${event.user} — ${STAGE_LABELS[event.stage] ?? event.stage}`,
        tone: "active",
      }),
    onRunFinished: (event) =>
      show(
        {
          text: event.status === "ok" ? "run finished — ok" : "run failed",
          tone: event.status === "ok" ? "ok" : "error",
        },
        true,
      ),
  });

  if (!activity) return null;

  return (
    <NavLink
      to="/runs"
      title="Live activity — click for the Runs page"
      className={cn(
        "mx-3 mb-1 flex items-center gap-2 rounded-lg border px-3 py-2 text-xs transition-colors",
        activity.tone === "active" &&
          "border-primary/40 bg-primary/10 text-foreground",
        activity.tone === "ok" && "border-success/40 bg-success/10",
        activity.tone === "error" && "border-destructive/40 bg-destructive/10",
      )}
    >
      {activity.tone === "active" ? (
        <Loader2
          className="h-3 w-3 shrink-0 animate-spin text-primary"
          aria-hidden="true"
        />
      ) : (
        <span
          aria-hidden="true"
          className={cn(
            "h-2 w-2 shrink-0 rounded-full",
            activity.tone === "ok" ? "bg-success" : "bg-destructive",
          )}
        />
      )}
      <span className="min-w-0 truncate">{activity.text}</span>
    </NavLink>
  );
}
