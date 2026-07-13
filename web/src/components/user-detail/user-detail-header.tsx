import { RefreshCw } from "lucide-react";

import { UserAvatar } from "@/components/user-avatar";
import { UserBadges } from "@/components/user-badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { formatHitRate, timeAgo } from "@/lib/format";
import { usePatchUser, useStartRun } from "@/lib/queries";
import type { User } from "@/lib/types";

/** The user page's identity header: avatar, status badges, stats, pause toggle, and Regenerate. */
export function UserDetailHeader({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const startRun = useStartRun();
  const paused = user.prefs?.paused ?? false;

  return (
    <div className="space-y-8">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex items-center gap-4">
          <UserAvatar name={user.username} size="lg" />
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="text-2xl font-semibold tracking-tight">
                {user.username}
              </h1>
              <UserBadges user={user} />
              {paused && <Badge variant="secondary">paused</Badge>}
            </div>
            <p className="text-sm text-muted-foreground">
              {user.history_depth} titles watched · last run{" "}
              {timeAgo(user.last_run_at)} · {formatHitRate(user.hit_rate)} of
              picks watched
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            {paused ? "Paused" : "Active"}
            <Switch
              checked={!paused}
              onCheckedChange={(active) =>
                patchUser.mutate({
                  id: user.id,
                  patch: { prefs: { paused: !active } },
                })
              }
              aria-label={`Pause or resume ${user.username}`}
            />
          </label>
          <Button
            variant="secondary"
            onClick={() => startRun.mutate({ user_ids: [user.id] })}
            loading={startRun.isPending}
          >
            {!startRun.isPending && <RefreshCw aria-hidden="true" />}
            Regenerate now
          </Button>
        </div>
      </header>

      {startRun.isSuccess && (
        <p className="text-sm text-muted-foreground">
          Run started — watch it live on the Dashboard.
        </p>
      )}
    </div>
  );
}
