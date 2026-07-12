import { Loader2, Play } from "lucide-react";
import { Link } from "react-router-dom";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { formatHitRate, timeAgo } from "@/lib/format";
import type { User } from "@/lib/types";

const POSTER_PLACEHOLDER_COUNT = 5;

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
  if (activeStage) return `Running: ${activeStage}…`;
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
        <CardTitle>
          <Link to={`/users/${user.id}`} className="rounded-sm hover:underline">
            {user.username}
          </Link>
        </CardTitle>
        <div className="flex items-center gap-2">
          {user.cold_start && <Badge variant="warning">cold start</Badge>}
          {user.hit_rate !== null && (
            <Badge variant="secondary">
              hit rate {formatHitRate(user.hit_rate)}
            </Badge>
          )}
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        <div aria-hidden="true" className="flex gap-1.5">
          {Array.from({ length: POSTER_PLACEHOLDER_COUNT }, (_, i) => (
            <div key={i} className="h-14 w-9 rounded-sm bg-muted" />
          ))}
        </div>
        <p className="min-h-5 text-sm text-muted-foreground">
          {statusLine(user, activeStage)}
        </p>
        <div className="flex items-center justify-between">
          <Button
            size="sm"
            variant="secondary"
            disabled={runPending || activeStage !== null || !user.enabled}
            onClick={() => onRunNow(user)}
          >
            {runPending || activeStage !== null ? (
              <Loader2 className="animate-spin" aria-hidden="true" />
            ) : (
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
              aria-label={`Rowarr row for ${user.username}`}
            />
          </div>
        </div>
      </CardContent>
    </Card>
  );
}
