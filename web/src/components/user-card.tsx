import { Play, Sparkles } from "lucide-react";
import { Link } from "react-router-dom";

import { UserAvatar } from "@/components/user-avatar";
import { UserBadges } from "@/components/user-badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { formatHitRate, timeAgo } from "@/lib/format";
import { STAGE_LABELS } from "@/lib/run-stages";
import type { User } from "@/lib/types";

export interface UserCardProps {
  user: User;
  /** Pipeline stage name while a run is in flight for this user, else null. */
  activeStage: string | null;
  /** True while the "Run now" request for this user is pending. */
  runPending: boolean;
  onRunNow: (user: User) => void;
  onToggleEnabled: (user: User, enabled: boolean) => void;
}

function statusLine(user: User, activeStage: string | null): string {
  // Map the raw SSE stage token ("candidates", "cataloguing"…) to its human label, matching the
  // run-detail log and activity pill — a bare token like "Running: candidates…" reads as jargon.
  if (activeStage)
    return `Running: ${STAGE_LABELS[activeStage] ?? activeStage}…`;
  if (!user.enabled) return "Turned off — no row is maintained for this user.";
  if (user.cold_start)
    return "Thin history — getting the popular-titles fallback row.";
  if (user.last_run_at) return `Row refreshed ${timeAgo(user.last_run_at)}.`;
  return "Never run yet.";
}

/** Dashboard card for one Plex user: poster strip, status, hit rate, controls. */
export function UserCard({
  user,
  activeStage,
  runPending,
  onRunNow,
  onToggleEnabled,
}: UserCardProps) {
  const switchId = `enable-${user.slug}`;
  return (
    <Card
      data-testid={`user-card-${user.slug}`}
      className={user.enabled ? "" : "opacity-60"}
    >
      <CardHeader className="flex-row items-center justify-between space-y-0 pb-3">
        <CardTitle className="flex items-center gap-2.5">
          <UserAvatar name={user.username} size="md" />
          <Link to={`/users/${user.id}`} className="rounded-sm hover:underline">
            {user.username}
          </Link>
        </CardTitle>
        <div className="flex items-center gap-2">
          <UserBadges user={user} />
          {user.hit_rate !== null && (
            <Badge
              variant="secondary"
              title="Share of Shortlist's picks this person has watched"
            >
              {formatHitRate(user.hit_rate)} watched
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {/* A real preview of what's in their row right now — their most recent pick titles. Falls
            back to a neutral placeholder before the first run. Dims with the card when turned off. */}
        <div className="rounded-lg border bg-gradient-to-br from-accent/40 to-elevated p-2.5">
          <div className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-primary">
            <Sparkles className="h-3 w-3" aria-hidden="true" />
            Recent picks
          </div>
          {user.preview_titles && user.preview_titles.length > 0 ? (
            <ul className="space-y-0.5">
              {user.preview_titles.map((title, i) => (
                <li key={i} className="truncate text-sm text-foreground/90">
                  {title}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">
              No picks yet — they’ll appear here after the first run.
            </p>
          )}
        </div>
        <p className="min-h-5 text-sm text-muted-foreground">
          {statusLine(user, activeStage)}
        </p>
        <div className="flex items-center justify-between">
          <Button
            size="sm"
            variant="secondary"
            loading={runPending || activeStage !== null}
            disabled={!user.enabled}
            onClick={() => onRunNow(user)}
          >
            {runPending || activeStage !== null ? null : (
              <Play aria-hidden="true" />
            )}
            Run now
          </Button>
          <div className="flex items-center gap-2">
            <label htmlFor={switchId} className="text-xs text-muted-foreground">
              {user.enabled ? "On" : "Off"}
            </label>
            <Switch
              id={switchId}
              checked={user.enabled}
              onCheckedChange={(checked) => onToggleEnabled(user, checked)}
              aria-label={`Shortlist row for ${user.username}`}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
