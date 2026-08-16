import {
  Image as ImageIcon,
  ListChecks,
  UserCheck,
  Users as UsersIcon,
} from "lucide-react";
import { Link } from "react-router";

import { RowDestructiveActions } from "@/components/rows/row-destructive-actions";
import { RowRunAction } from "@/components/rows/row-run-action";
import { RowEnableToggle } from "@/components/rows/row-enable-toggle";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api } from "@/lib/api";
import { audienceSummary, rowOverrides } from "@/lib/collections";
import { DEFAULT_ROW_SLUG } from "@/lib/constants";
import { settingString } from "@/lib/format";
import { useLibraries, useSettings } from "@/lib/queries";
import type { Collection, User } from "@/lib/types";
import { cn } from "@/lib/utils";

/** A row name with its `{placeholders}` shown as placeholders rather than as literal text.
 *
 * The list used to print the raw template — "✨ {library_name} Picked for You" — beside cards
 * whose names had no token in them, and beside a dashboard that shows the same row resolved
 * ("✨ Movies Picked for You"). Read cold it looks like a substitution that failed. Resolving it
 * here instead would be a different lie: a row over two libraries really is two collections with
 * two names, so there is no single name to show. Marking the variable as a variable is the honest
 * version, and it costs one chip.
 */
// EXACTLY the tokens the engine substitutes (`delivery.py`), matched case-sensitively — not
// `\{[a-z_]+\}`. The name field is free text and nothing validates a token whitelist, so a loose
// pattern would dress "Best of {genre}" or "{Library_Name} Picks" up as a resolved placeholder
// while Plex receives the literal braces in the collection title on every home screen. Anything
// outside this set must stay plain text: the whole point is to stop a template reading as a failed
// substitution, and hiding a typo does the exact opposite.
const ROW_NAME_TOKEN_SPLIT = /(\{(?:user|top_seed|library_name)\})/;
const ROW_NAME_TOKEN = /^\{(?:user|top_seed|library_name)\}$/;

function RowCardName({ name }: { name: string }) {
  const parts = name.split(ROW_NAME_TOKEN_SPLIT);
  return (
    <span className="font-medium">
      {parts.map((part, i) =>
        ROW_NAME_TOKEN.test(part) ? (
          <span
            key={i}
            className="mx-0.5 rounded bg-muted px-1 py-0.5 text-xs font-normal text-muted-foreground"
          >
            {part.slice(1, -1).replace(/_/g, " ")}
          </span>
        ) : (
          part
        ),
      )}
    </span>
  );
}

/** One row in the Rows list: its audience/size summary, an enable toggle, edit, and delete.
 *
 * Renaming is NOT here. It lives in the editor beside the name it changes, which is where someone
 * looking to rename a row goes anyway — and on a card it was a third destructive-ish Plex write
 * competing for space with the two that had to stay.
 */
export function RowCard({
  collection,
  users,
  onEdit,
}: {
  collection: Collection;
  users: User[];
  onEdit: () => void;
}) {
  const settings = useSettings();
  const libraries = useLibraries();
  const isDefault = collection.slug === DEFAULT_ROW_SLUG;
  // Turning a row OFF takes its collections off everyone's Plex on the next run
  // (`rows._remove_muted_and_retired`), which a toggle gives no hint of. Turning it back ON is
  // harmless and stays a single click.
  // null until the library list actually arrives — a half-loaded card must not label a row's
  // libraries with raw Plex section keys, which mean nothing to the owner.
  const overrides = rowOverrides(
    collection,
    libraries.isSuccess ? libraries.data : null,
    settings.data,
  );

  // The default row's size is delivered from Settings → Defaults, not its own column (which the
  // backend ignores). Show the effective value so the card can't advertise a size no user gets.
  const globalSize = Number(settingString(settings.data ?? {}, "row.size"));
  const effectiveSize =
    isDefault && Number.isFinite(globalSize) && globalSize > 0
      ? globalSize
      : collection.size;

  return (
    <Card className={cn(!collection.enabled && "opacity-60")}>
      <CardContent className="flex flex-wrap items-center justify-between gap-4 pt-6">
        {/* The slot is always here, poster or not — otherwise a row without one loses 11rem of
            leading space and its name no longer lines up with every other card in the list. */}
        {collection.poster?.has_image ? (
          <img
            // Cache-bust on everything that changes the rendered image, so editing a text poster's
            // title/style refreshes the thumbnail instead of showing the stale one.
            src={`${api.posterImageUrl(collection.id)}?v=${encodeURIComponent(
              [
                collection.poster.mode,
                collection.poster.title,
                collection.poster.subtitle,
                collection.poster.style,
              ].join("|"),
            )}`}
            alt=""
            aria-hidden="true"
            className="h-16 w-11 shrink-0 rounded border object-cover"
          />
        ) : (
          <div
            aria-hidden="true"
            title="No poster — Plex uses its own artwork for this row"
            className="flex h-16 w-11 shrink-0 items-center justify-center rounded border border-dashed bg-muted/40"
          >
            <ImageIcon className="size-4 text-muted-foreground/60" />
          </div>
        )}
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <RowCardName name={collection.name} />
            <Badge
              variant={collection.build === "shared" ? "warning" : "secondary"}
            >
              {collection.build === "shared" ? (
                <UsersIcon className="h-3 w-3" aria-hidden="true" />
              ) : (
                <UserCheck className="h-3 w-3" aria-hidden="true" />
              )}
              {collection.build === "shared" ? "Shared" : "Per person"}
            </Badge>
            {isDefault && <Badge variant="outline">default</Badge>}
          </div>
          <p className="text-sm text-muted-foreground">
            {audienceSummary(collection, users)} · {effectiveSize} titles ·{" "}
            {collection.media === "both"
              ? "movies & shows"
              : `${collection.media}s`}
          </p>
          {overrides.length > 0 && (
            <div className="flex flex-wrap gap-1 pt-0.5">
              {overrides.map((part) => (
                <Badge key={part} variant="outline" className="font-normal">
                  {part}
                </Badge>
              ))}
            </div>
          )}
        </div>
        {/* Wraps: six controls (toggle, Run, Runs, Edit, Remove from Plex, Delete) need well over
            500px in one line, so on a phone they ran off the screen and Delete was unreachable.
            Wrapping costs a row of height on narrow screens and changes nothing above it. */}
        <div className="flex flex-wrap items-center justify-end gap-2">
          <RowEnableToggle collection={collection} />
          {/* Rebuild, then the history of rebuilding — "Run" beside "Runs" in that order, because
              the answer to "did that work?" is the screen the Run button already sends you to. */}
          <RowRunAction collection={collection} />
          <Button
            asChild
            variant="ghost"
            size="sm"
            className="text-muted-foreground"
            title="See the runs that built this row"
          >
            <Link to={`/runs?row=${encodeURIComponent(collection.slug)}`}>
              <ListChecks aria-hidden="true" />
              Runs
            </Link>
          </Button>
          <Button variant="outline" size="sm" onClick={onEdit}>
            Edit
          </Button>
          <RowDestructiveActions collection={collection} />
        </div>
      </CardContent>
    </Card>
  );
}
