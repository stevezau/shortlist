import { useState } from "react";
import { Link } from "react-router-dom";

import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { MutationAlert } from "@/components/mutation-alert";
import { PickList } from "@/components/pick-list";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { RowSizeField } from "@/components/row-size-field";
import { SaveStatus } from "@/components/save-status";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { useAutosave } from "@/lib/autosave";
import { useSetUserRowOverride, useUserRows } from "@/lib/queries";
import type { User, UserRow } from "@/lib/types";

/** One of a person's rows: its live picks, and a per-person customization drawer. */
function UserRowCard({ userId, row }: { userId: number; row: UserRow }) {
  // Two mutations on purpose: the mute switch and the drawer fail independently, and a failed mute
  // must never be reported (or hidden) as a failed customization.
  const mute = useSetUserRowOverride(userId);
  const save = useSetUserRowOverride(userId);
  const [open, setOpen] = useState(false);
  const [size, setSize] = useState<string>(
    row.override.row_size ? String(row.override.row_size) : "default",
  );
  const [curation, setCuration] = useState<CurationStyleValue>({
    tone: row.override.prompt_tone,
    guidance: row.override.prompt_guidance,
    template: row.override.prompt_template,
  });
  const [saved, setSaved] = useState(false);

  // Muted is what the SERVER says, with the in-flight value laid over it only while the PUT is
  // actually in flight. This card is a privacy claim: an optimistic local flag meant a rejected
  // mute left it reading "muted", dimmed, switch off — while Plex was still delivering that row to
  // the person. On failure it now snaps back to the truth, and says so.
  const muted =
    (mute.isPending ? mute.variables?.patch.muted : undefined) ?? row.muted;

  // The mute sends ONLY {muted} so it can never persist half-typed drawer edits; the drawer sends
  // only size + curation (the server writes just the fields it receives).
  const setMuted = (nextMuted: boolean) =>
    mute.mutate({
      collectionId: row.collection_id,
      patch: { muted: nextMuted },
    });

  // The drawer auto-saves like every other section of the app, so collapsing it ("Hide
  // customization" — which sounds harmless) or walking away can't silently discard an edit.
  const retrySave = useAutosave({ size, curation }, () => {
    setSaved(false);
    save.mutate(
      {
        collectionId: row.collection_id,
        patch: {
          row_size: size === "default" ? null : Number(size),
          prompt_tone: curation.tone,
          prompt_guidance: curation.guidance,
          prompt_template: curation.template,
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
              <span className="font-medium">{row.name}</span>
              {row.is_default && <Badge variant="outline">default</Badge>}
              {muted && <Badge variant="secondary">muted</Badge>}
            </div>
            <p className="text-sm text-muted-foreground">
              {row.override.row_size ?? row.size} titles ·{" "}
              {row.media === "both" ? "movies & shows" : `${row.media}s`}
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
            <PickList picks={row.picks} />
          ) : (
            <p className="text-sm text-muted-foreground">
              No picks in this row yet — regenerate below or wait for the next
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
              <div className="space-y-2">
                <p className="text-sm font-medium">Curation style</p>
                <CurationStyleFields
                  value={curation}
                  onChange={setCuration}
                  allowInherit
                  scope="user"
                />
              </div>
            </div>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/** All the rows that reach one user, each with its picks and per-person customization. */
export function UserRowsSection({ user }: { user: User }) {
  const query = useUserRows(user.id);
  return (
    <QueryBoundary
      query={query}
      skeleton={<Skeleton className="h-40 w-full" />}
      isEmpty={(rows) => rows.length === 0}
      empty={
        <EmptyState
          title="No rows reach this person"
          hint="They aren’t in the audience of any row yet. Add a row, or set its audience to include them, on the Rows page."
          action={
            <Button asChild variant="outline" size="sm">
              <Link to="/rows">Go to Rows</Link>
            </Button>
          }
        />
      }
    >
      {(rows) => (
        <div className="space-y-3">
          {rows.map((row) => (
            <UserRowCard key={row.collection_id} userId={user.id} row={row} />
          ))}
        </div>
      )}
    </QueryBoundary>
  );
}
