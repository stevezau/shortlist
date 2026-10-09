import { useState } from "react";
import { Link } from "react-router";

import { MutationAlert } from "@/components/mutation-alert";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { RecentCountField } from "@/components/recent-count-field";
import { RowSizeField } from "@/components/row-size-field";
import { SaveStatus } from "@/components/save-status";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { GroupedPicks } from "@/components/user-detail/grouped-picks";
import { useAutosave } from "@/lib/autosave";
import { LIBRARY_NAME } from "@/lib/placeholders";
import { resolveRowName } from "@/lib/run-rows";
import { useSetUserRowOverride, useUserRows } from "@/lib/queries";
import type { User, UserRow } from "@/lib/types";
import { personName } from "@/lib/user-names";
import { userState } from "@/lib/user-state";

/** The title an owner reads for one of a person's rows: this library and this person's lead seed
 *  where they exist, plain words where the row has built nothing yet. */
function personRowName(row: UserRow, person: string): string {
  const template =
    row.library && !row.name.includes(LIBRARY_NAME)
      ? `${row.name} — ${row.library}`
      : row.name;
  const topSeed = row.picks.find((pick) => pick.seed_title)?.seed_title ?? undefined;
  return resolveRowName(template, { library: row.library || undefined, topSeed, user: person });
}

/** One of a person's rows: its live picks, and a per-person customization drawer. */
function UserRowCard({ userId, name, row }: { userId: number; name: string; row: UserRow }) {
  // Two mutations on purpose: the mute switch and the drawer fail independently, and a failed mute
  // must never be reported (or hidden) as a failed customization.
  const mute = useSetUserRowOverride(userId);
  const save = useSetUserRowOverride(userId);
  const [open, setOpen] = useState(false);
  const [size, setSize] = useState<string>(
    row.override.row_size ? String(row.override.row_size) : "default",
  );
  const [recent, setRecent] = useState<string>(
    row.override.recent_count ? String(row.override.recent_count) : "default",
  );
  const [saved, setSaved] = useState(false);

  // Muted is what the SERVER says, with the in-flight value laid over it only while the PUT is
  // actually in flight. This card is a privacy claim: an optimistic local flag meant a rejected
  // mute left it reading "muted", dimmed, switch off — while Plex was still delivering that row to
  // the person. On failure it now snaps back to the truth, and says so.
  const muted =
    (mute.isPending ? mute.variables?.patch.muted : undefined) ?? row.muted;

  // This person's override if they have one, else the row's own size. The ceiling, not a count.
  const configuredSize = row.override.row_size ?? row.size;

  // The mute sends ONLY {muted} so it can never persist a half-changed size; the drawer sends only
  // the size (the server writes just the fields it receives).
  const setMuted = (nextMuted: boolean) =>
    mute.mutate({
      collectionId: row.collection_id,
      patch: { muted: nextMuted },
    });

  // The drawer auto-saves like every other section of the app, so collapsing it ("Hide
  // customization" — which sounds harmless) or walking away can't silently discard an edit. Both
  // knobs ride the one PUT; "default" clears that field's override back to the row's own setting.
  const retrySave = useAutosave({ size, recent }, () => {
    setSaved(false);
    save.mutate(
      {
        collectionId: row.collection_id,
        patch: {
          row_size: size === "default" ? null : Number(size),
          recent_count: recent === "default" ? null : Number(recent),
        },
      },
      { onSuccess: () => setSaved(true) },
    );
  });

  return (
    <Card className={muted ? "opacity-60" : ""}>
      <CardContent className="space-y-4 pt-6">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              {/* This card is one library's copy of the row, so `{library_name}` has exactly one
                  value here — and a name that carries it already says which library, so the
                  suffix stays only for a name that does not. */}
              <span className="font-medium">{personRowName(row, name)}</span>
              {row.is_default && <Badge variant="outline">default</Badge>}
              {muted && <Badge variant="secondary">muted</Badge>}
            </div>
            {/* The configured size is a CEILING, and printing it bare put "15 titles" directly
                above a list offering "Show all 10 (+5)" — one number apparently disagreeing with
                itself. Say which is which: what they actually got, out of what the row allows.

                `picks.length <= configuredSize` is the load-bearing half. `picks` came from the
                LAST RUN and `configuredSize` is the setting as it stands now, so lowering the size
                from 15 to 5 refetches this card immediately and would otherwise read "15 of 5
                titles" until the next run rebuilt the row. The ceiling alone is honest there. */}
            <p className="text-sm text-muted-foreground">
              {!muted &&
              row.picks.length > 0 &&
              row.picks.length <= configuredSize
                ? `${row.picks.length} of ${configuredSize} titles`
                : `up to ${configuredSize} titles`}{" "}
              · {row.media === "both" ? "movies & shows" : `${row.media}s`}
            </p>
          </div>
          <label className="flex items-center gap-2 text-sm text-muted-foreground">
            {muted ? "Off for them" : "On"}
            <Switch
              checked={!muted}
              onCheckedChange={(on) => setMuted(!on)}
              aria-label={`Show ${row.name} for this person`}
            />
          </label>
        </div>

        {/* Always visible — never inside the collapsed drawer. A rejected mute is a privacy fact
            about what this person can still see, so it can't be hidden behind a disclosure. */}
        {mute.isError && (
          <MutationAlert
            error={mute.error}
            lead={
              row.muted
                ? `“${row.name}” is still muted for this person.`
                : `“${row.name}” is still showing for this person.`
            }
            fallback="Couldn’t change that. Check the server log and try again."
            onRetry={() => {
              const last = mute.variables;
              if (last) mute.mutate(last);
            }}
          />
        )}

        {!muted &&
          (row.picks.length > 0 ? (
            <GroupedPicks picks={row.picks} collapseAfter={10} />
          ) : (
            <p className="text-sm text-muted-foreground">
              No picks in this row yet — use Run now above, or wait for the next
              run.
            </p>
          ))}

        <div className="border-t pt-3">
          {/* The save state lives OUTSIDE the drawer: an auto-save can land (or fail) after the
              drawer is collapsed, and a failure hidden behind a disclosure isn't a failure shown. */}
          <div className="flex flex-wrap items-center gap-3">
            <Button
              variant="ghost"
              size="sm"
              className="-ml-3"
              onClick={() => setOpen((v) => !v)}
              aria-expanded={open}
            >
              {open ? "Hide customization" : "Customize for this person"}
            </Button>
            <SaveStatus
              isPending={save.isPending}
              isError={save.isError}
              error={save.error}
              saved={saved}
              onRetry={retrySave}
              fallback="Couldn’t save this person’s customization. Try again."
            />
          </div>

          {open && (
            <div className="mt-3 space-y-4">
              <div className="space-y-2">
                <label className="flex items-center gap-2 text-sm font-medium">
                  <Switch
                    checked={size !== "default"}
                    onCheckedChange={(on) =>
                      setSize(on ? String(row.size) : "default")
                    }
                  />
                  Custom row size for this person
                </label>
                {size === "default" ? (
                  <p className="text-sm text-muted-foreground">
                    Using this row&rsquo;s size ({row.size} titles).
                  </p>
                ) : (
                  <RowSizeField
                    value={Number(size)}
                    onChange={(next) => setSize(String(next))}
                    label="Titles for this person"
                  />
                )}
              </div>

              <div className="space-y-2 border-t pt-4">
                <label className="flex items-center gap-2 text-sm font-medium">
                  <Switch
                    checked={recent !== "default"}
                    onCheckedChange={(on) =>
                      setRecent(on ? String(row.recent_count) : "default")
                    }
                  />
                  Custom watch-history depth for this person
                </label>
                <p className="text-sm text-muted-foreground">
                  How many of this person&rsquo;s most recent watches the AI
                  web-search source looks up for this row (one cached search
                  each). Only affects rows using AI web search.
                </p>
                {recent === "default" ? (
                  <p className="text-sm text-muted-foreground">
                    Using this row&rsquo;s depth ({row.recent_count} recent
                    watches).
                  </p>
                ) : (
                  <RecentCountField
                    value={Number(recent)}
                    onChange={(next) => setRecent(String(next))}
                    label="Recent watches for this person"
                  />
                )}
              </div>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/**
 * An Off person's rows: listed, but plainly not applying. Their switches are shown off and locked,
 * because nothing new is built for them and these controls would otherwise claim it is. A row still
 * on Plex (it has live picks) keeps showing them: Off stops new rows, it does not take the old ones down.
 */
function OffRowsList({ rows, name }: { rows: UserRow[]; name: string }) {
  return (
    <Card>
      <ul className="divide-y">
        {rows.map((row) => (
          <li key={`${row.collection_id}-${row.section_key}`} className="space-y-3 px-6 py-4">
            <div className="flex items-center justify-between gap-4 opacity-60">
              <div>
                <div className="font-medium text-muted-foreground">{personRowName(row, name)}</div>
                <div className="text-sm text-faint-foreground">
                  {row.media === "both" ? "movies & shows" : `${row.media}s`}
                </div>
              </div>
              <label className="flex items-center gap-4 text-sm text-muted-foreground">
                No new rows &mdash; {name} is off
                <Switch checked={false} disabled aria-label={`${row.name} does not apply while ${name} is off`} />
              </label>
            </div>
            {row.picks.length > 0 && <GroupedPicks picks={row.picks} collapseAfter={10} />}
          </li>
        ))}
      </ul>
    </Card>
  );
}

/** All the rows that reach one user, each with its picks and per-person customization. */
export function UserRowsSection({ user }: { user: User }) {
  const query = useUserRows(user.id);
  const off = userState(user) === "off";
  const name = personName(user);
  return (
    <QueryBoundary
      query={query}
      skeleton={<Skeleton className="h-40 w-full" />}
      isEmpty={(rows) => rows.length === 0}
      empty={
        <EmptyState
          title="No personal rows for this person"
          hint="No per-person row includes them yet. Create one, or widen an existing row’s audience to cover them, on the Rows page. (Shared rows — the ones everybody sees — aren’t listed here.)"
          action={
            <Button asChild variant="outline" size="sm">
              <Link to="/rows">Go to Rows</Link>
            </Button>
          }
        />
      }
    >
      {(rows) =>
        off ? (
          <OffRowsList rows={rows} name={name} />
        ) : (
          <div className="space-y-3">
            {rows.map((row) => (
              <UserRowCard key={row.collection_id} userId={user.id} name={name} row={row} />
            ))}
          </div>
        )
      }
    </QueryBoundary>
  );
}
