import { useState } from "react";
import { Link } from "react-router-dom";

import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { PickList } from "@/components/pick-list";
import { QueryBoundary, EmptyState } from "@/components/query-boundary";
import { SavedIndicator } from "@/components/saved-indicator";
import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import { apiErrorMessage } from "@/lib/api";
import { useSetUserRowOverride, useUserRows } from "@/lib/queries";
import type { User, UserRow } from "@/lib/types";

const SIZE_OPTIONS = [
  { value: "default", label: "Default" },
  { value: "10", label: "10" },
  { value: "15", label: "15" },
  { value: "20", label: "20" },
];

/** One of a person's rows: its live picks, and a per-person customization drawer. */
function UserRowCard({ userId, row }: { userId: number; row: UserRow }) {
  const save = useSetUserRowOverride(userId);
  const [open, setOpen] = useState(false);
  const [muted, setMuted] = useState(row.muted);
  const [size, setSize] = useState<string>(
    row.override.row_size ? String(row.override.row_size) : "default",
  );
  const [curation, setCuration] = useState<CurationStyleValue>({
    tone: row.override.prompt_tone,
    guidance: row.override.prompt_guidance,
    template: row.override.prompt_template,
  });
  const [saved, setSaved] = useState(false);

  // The mute switch sends ONLY {muted} so it can't persist unsaved drawer edits; the drawer's
  // Save button sends size + curation (the server only writes the fields it receives).
  const setMutedOnly = (nextMuted: boolean) => {
    setMuted(nextMuted);
    save.mutate({
      collectionId: row.collection_id,
      patch: { muted: nextMuted },
    });
  };

  const saveCustomization = () => {
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
  };

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
              onCheckedChange={(on) => setMutedOnly(!on)}
              aria-label={`Show ${row.name} for this person`}
            />
          </label>
        </div>

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
          <Button
            variant="ghost"
            size="sm"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            {open ? "Hide customization" : "Customize for this person"}
          </Button>

          {open && (
            <div className="mt-3 space-y-4">
              <Segmented
                legend="Row size"
                value={size}
                options={SIZE_OPTIONS}
                onChange={setSize}
              />
              <div className="space-y-2">
                <p className="text-sm font-medium">Curation style</p>
                <p className="text-sm text-muted-foreground">
                  Leave a field blank to use this row&rsquo;s own style.
                </p>
                <CurationStyleFields value={curation} onChange={setCuration} />
              </div>
              <div className="flex items-center gap-3">
                <Button
                  size="sm"
                  onClick={saveCustomization}
                  loading={save.isPending}
                >
                  Save
                </Button>
                <SavedIndicator show={saved && !save.isPending} as="span" />
                {save.isError && (
                  <span role="alert" className="text-sm text-destructive">
                    {apiErrorMessage(save.error, "Save failed.")}
                  </span>
                )}
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
