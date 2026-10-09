import { ExternalLink, RefreshCw } from "lucide-react";
import { Link } from "react-router";

import { MutationAlert } from "@/components/mutation-alert";
import { PLEX_USERS_URL } from "@/components/user-detail/off-banner";
import { UserAvatar } from "@/components/user-avatar";
import { UserBadges } from "@/components/user-badges";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Switch } from "@/components/ui/switch";
import { timeAgo } from "@/lib/format";
import { usePatchUser, useStartRun } from "@/lib/queries";
import type { User } from "@/lib/types";
import { profileBlocksRows, userState } from "@/lib/user-state";
import { personName } from "@/lib/user-names";

/** The user page's identity header: avatar, status badges, stats, pause toggle, and Run now. */
export function UserDetailHeader({ user }: { user: User }) {
  const patchUser = usePatchUser();
  const startRun = useStartRun();
  // The same state the Users list shows. `enabled` (does this person get a Shortlist row at all) and
  // `paused` (temporarily skipped on runs) are different switches; Off makes Paused moot, so an Off
  // person never shows an "Active" control that lies.
  const state = userState(user);
  const off = state === "off";
  const paused = state === "paused";
  const name = personName(user);
  const profileBlocked = profileBlocksRows(user);
  const offReason = profileBlocked
    ? `Clear ${name}’s Restriction Profile in Plex first`
    : `Turn ${name} on first`;

  return (
    <div className="space-y-4">
      <header className="flex flex-wrap items-center justify-between gap-4">
        <div className="flex w-full min-w-0 items-start gap-3 lg:w-auto lg:flex-1">
          <UserAvatar name={user.username} size="lg" />
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-3">
              <h1 className="min-w-0 break-words text-2xl font-semibold tracking-tight">
                {/* The breadcrumb shares the title line, so this page's title sits at the same height as
                    every other page's. */}
                <Link to="/users" className="font-normal text-muted-foreground hover:text-foreground">
                  Users
                </Link>
                <span className="font-normal text-faint-foreground">{" / "}</span>
                {name}
              </h1>
              <UserBadges user={user} />
              {off && <Badge variant="secondary">off</Badge>}
              {paused && <Badge variant="secondary">paused</Badge>}
            </div>
            <p className="break-words text-sm text-muted-foreground">
              {user.display_name && user.display_name !== user.username && (
                <>Plex username: {user.username} · </>
              )}
              {user.history_depth} titles watched · last run{" "}
              {timeAgo(user.last_run_at)}
              {off && " · no new rows"}
              {/* Dropped, never printed as "· — picks watched": in a table cell an em dash reads as
                  "nothing to report", but in a sentence it is a hole. */}
              {user.picks_watched_30d !== null ? (
                <>
                  {" "}
                  · {user.picks_watched_30d}{" "}
                  {user.picks_watched_30d === 1 ? "pick" : "picks"} watched in
                  30 days
                </>
              ) : null}
            </p>
          </div>
        </div>
        <div className="flex flex-wrap items-start gap-x-4 gap-y-2">
          {!off && (
            <label
              className="flex items-center gap-3 text-sm"
              title="Pausing takes this person off Home and skips them on runs. Nothing is deleted; resuming puts their rows back."
            >
              <span className="font-medium">Active</span>
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
              <span className="text-muted-foreground">
                {paused
                  ? "Paused: their rows are off Home"
                  : "Pausing takes their rows off Home; nothing is deleted"}
              </span>
            </label>
          )}
          <div className="text-right">
            <Button
              variant="secondary"
              onClick={() => startRun.mutate({ user_ids: [user.id] })}
              loading={startRun.isPending}
              disabled={off}
              aria-label={`Run for ${name}`}
              title={`Rebuilds only ${name}'s rows, just for them — no one else is touched.`}
            >
              {!startRun.isPending && <RefreshCw aria-hidden="true" />}
              Run now
            </Button>
            {off && <p className="mt-1 text-xs text-muted-foreground">{offReason}</p>}
          </div>
          {/* "Turn on" only where it changes something. A Restriction Profile keeps the person Off
              whatever `enabled` says (`userState`), so a Turn on there would save and still read Off:
              the honest action is the fix in Plex. */}
          {off && !profileBlocked && (
            <Button
              onClick={() => patchUser.mutate({ id: user.id, patch: { enabled: true } })}
              loading={patchUser.isPending}
            >
              Turn on
            </Button>
          )}
          {off && profileBlocked && (
            <Button asChild>
              <a href={PLEX_USERS_URL} target="_blank" rel="noreferrer">
                Fix in Plex
                <ExternalLink aria-hidden="true" />
              </a>
            </Button>
          )}
        </div>
      </header>

      {/* Runs are watched on the Runs page — the Dashboard is the watch-tracking report and shows
          nothing live, so pointing there sent people somewhere the run never appears. */}
      {startRun.isSuccess && (
        <p className="text-sm text-muted-foreground">
          Run started for {name} only &mdash;
          follow it on{" "}
          <Link to="/runs" className="font-medium underline">
            Runs
          </Link>
          .
        </p>
      )}

      {/* A run that fails to start says exactly why; it used to be dropped, leaving the button
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
          lead={off ? "They are still off." : paused ? "They are still paused." : "They are still active."}
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
