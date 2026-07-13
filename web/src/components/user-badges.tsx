import type { ReactNode } from "react";

import { Badge } from "@/components/ui/badge";
import type { User } from "@/lib/types";

/**
 * The canonical status badges for a Plex user — managed / owner / cold-start — in one place so the
 * wording and tooltips stay identical across the dashboard, the users table, and the user page.
 * Renders `emptyFallback` (e.g. an em dash) when none apply.
 */
export function UserBadges({
  user,
  emptyFallback = null,
}: {
  user: User;
  emptyFallback?: ReactNode;
}) {
  const badges: ReactNode[] = [];
  if (user.user_type === "managed") {
    badges.push(
      <Badge key="managed" variant="secondary">
        managed
      </Badge>,
    );
  }
  if (user.user_type === "owner") {
    badges.push(
      <Badge
        key="owner"
        variant="outline"
        title="Plex cannot hide rows from the server owner"
      >
        owner
      </Badge>,
    );
  }
  if (user.cold_start) {
    badges.push(
      <Badge
        key="cold-start"
        variant="warning"
        title="Not enough watch history yet — starting from popular titles"
      >
        cold start
      </Badge>,
    );
  }
  if (badges.length === 0) return <>{emptyFallback}</>;
  return <>{badges}</>;
}
