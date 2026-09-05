import {
  CircleAlert,
  CircleCheck,
  type LucideIcon,
  TriangleAlert,
} from "lucide-react";
import { Link } from "react-router";

import { QueryBoundary } from "@/components/query-boundary";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import {
  CORE_CATEGORIES,
  deriveHealthChips,
  type HealthState,
} from "@/lib/health";
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
 * A green chip means "nothing outstanding here", not "checked and healthy": most of these alerts
 * are dismissable, and the Verdict card's status line below reads raw report fields no dismissal
 * can silence. Where the two overlap, that line is the authority — hence the wording here.
 */
export function HealthStrip() {
  const notifications = useNotifications();
  // The spacing lives on the wrapper so all three states — skeleton, error, chips — clear the
  // report below by the same amount.
  return (
    <div className="mb-6">
      <QueryBoundary
        query={notifications}
        skeleton={
          <ul className="flex flex-wrap gap-2">
            {CORE_CATEGORIES.map((core) => (
              <li key={core.category}>
                <Skeleton className="h-6 w-24 rounded-full" />
              </li>
            ))}
          </ul>
        }
      >
        {(data) => (
          <ul aria-label="Open alerts by area" className="flex flex-wrap gap-2">
            {deriveHealthChips(data.notifications).map((chip) => {
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
                        {chip.detail
                          ? `: ${chip.detail}`
                          : ": nothing outstanding"}
                      </span>
                    </Badge>
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </QueryBoundary>
    </div>
  );
}
