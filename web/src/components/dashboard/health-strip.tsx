import {
  CircleAlert,
  CircleCheck,
  type LucideIcon,
  TriangleAlert,
} from "lucide-react";
import { Link } from "react-router";

import { QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { deriveHealthChips, type HealthState } from "@/lib/health";
import { useNotifications } from "@/lib/queries";
import { cn } from "@/lib/utils";

/** Shape carries the state as well as colour — six chips told apart by hue alone are unreadable to
 *  anyone colour-blind, and the same three icons the bell uses keep one vocabulary. The hover tint
 *  is what says these are links rather than decoration. */
const TONE: Record<HealthState, { icon: LucideIcon; className: string }> = {
  ok: {
    icon: CircleCheck,
    className:
      "border-success/30 bg-success/10 text-success hover:bg-success/20",
  },
  warning: {
    icon: TriangleAlert,
    className:
      "border-warning/30 bg-warning/10 text-warning hover:bg-warning/20",
  },
  error: {
    icon: CircleAlert,
    className:
      "border-destructive/40 bg-destructive/10 text-destructive-text hover:bg-destructive/20",
  },
};

/**
 * One chip per subsystem the notification registry already watches, so the dashboard answers "which
 * area needs me" without the bell being opened and every alert read in order.
 *
 * ONLY renders chips that need attention. A healthy server shows nothing at all.
 *
 * Six permanently-green chips were an all-clear nobody asked for, rendered on every page load —
 * the exact thing `notifications-design.md` refuses for the webhook ("skipped entirely if empty; no
 * 'all clear' ping nobody asked for") and warns about for the update notice ("the paradigm case of
 * a notifier training itself to be ignored").
 *
 * They were also weaker than they looked. A green chip means "nothing outstanding here", NOT
 * "checked and healthy" — most of these alerts are dismissable, so dismissing one turns the chip
 * green while the fact behind it stands. And the Verdict card 400px below reads raw report fields
 * that no dismissal can silence, so on the two facts they share, that line is the authority and
 * this was the copy that had to hedge. A strip that is silent when everything is fine keeps the
 * "which area" grouping exactly when it is worth something and says nothing the bell has not
 * already said when it is not.
 */
export function HealthStrip() {
  const notifications = useNotifications();
  // The spacing lives on the wrapper so all three states — skeleton, error, chips — clear the
  // report below by the same amount.
  return (
    <div className="mb-6">
      <QueryBoundary
        query={notifications}
        // No skeleton: the common answer is "nothing to show", so a placeholder row would flash
        // six grey pills and then collapse on every dashboard load.
        skeleton={null}
      >
        {(data) => {
          const chips = deriveHealthChips(data.notifications).filter(
            (chip) => chip.state !== "ok",
          );
          if (chips.length === 0) return null;
          return (
          <ul aria-label="Needs attention" className="flex flex-wrap gap-2">
            {chips.map((chip) => {
              const { icon: Icon, className } = TONE[chip.state];
              return (
                <li key={chip.category}>
                  <Link
                    to={chip.href}
                    title={chip.detail ?? "Nothing outstanding"}
                    className="inline-block rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background"
                  >
                    <Badge
                      variant="outline"
                      className={cn("gap-1.5 py-1", className)}
                    >
                      <Icon
                        className="h-3.5 w-3.5 shrink-0"
                        aria-hidden="true"
                      />
                      {chip.label}
                      {/* The state itself, for anyone who cannot see the colour or the icon. */}
                      <span className="sr-only">
                        {chip.detail ? `: ${chip.detail}` : ""}
                      </span>
                    </Badge>
                  </Link>
                </li>
              );
            })}
          </ul>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
