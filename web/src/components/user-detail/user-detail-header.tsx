import { RefreshCw } from "lucide-react";
import { Link } from "react-router-dom";

import { MutationAlert } from "@/components/mutation-alert";
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
  // Two distinct states, kept from contradicting each other: `enabled` (does this person get a
  // Shortlist row at all — the Users-list On/Off) vs `paused` (temporarily skipped on runs, row kept).
  // When they're OFF, "paused" is moot, so we show the off state instead of an "Active" that lies.

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
              {!user.enabled && <Badge variant="secondary">off</Badge>}
              {user.enabled && paused && (
                <Badge variant="secondary">paused</Badge>
              )}
            </div>
            <p className="text-sm text-muted-foreground">
              {user.history_depth} titles watched · last run{" "}
              {timeAgo(user.last_run_at)} · {formatHitRate(user.hit_rate)} of
              picks watched
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {!user.enabled ? (
            // Off entirely — pausing is moot. Point at the one switch that turns them back on.
            <p className="text-sm text-muted-foreground">
              Turned off — no Shortlist row.{" "}
              <Link to="/users" className="font-medium underline">
                Turn on from Users
              </Link>
              .
            </p>
          ) : (
            <label
              className="flex items-center gap-2 text-sm text-muted-foreground"
              title="Pausing skips this person on runs but keeps their row — unlike turning them off on the Users list."
            >
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
          )}
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

      {/* A run the write gate refuses says exactly why; it used to be dropped, leaving the button
          simply stopping. */}
      {startRun.isError && (
        <MutationAlert
          error={startRun.error}
          fallback="Couldn’t start that run. Check the server log and try again."
        />
      )}

      {patchUser.isError && (
        <MutationAlert
          error={patchUser.error}
          lead={paused ? "They are still paused." : "They are still active."}
          fallback="Couldn’t save that change. Try again."
          onRetry={() => {
            const last = patchUser.variables;
            if (last) patchUser.mutate(last);
          }}
        />
      )}
    </div>
  );
}
