import { useState } from "react";

import { PLACEHOLDER_SPLIT, THEME, THEME_EMOJI } from "@/lib/placeholders";
import {
  Image as ImageIcon,
  ListChecks,
  Pencil,
  Sparkles,
  TextCursorInput,
  Trash2,
} from "lucide-react";
import { Link } from "react-router";

import { OverflowMenu } from "@/components/rows/overflow-menu";
import { builtAt, mediaLabel, peopleCount, rowKindTitle, rowReach } from "@/components/rows/row-facts";
import { RowRunAction } from "@/components/rows/row-run-action";
import { RowEnableToggle } from "@/components/rows/row-enable-toggle";
import { RowName } from "@/components/rows/row-name";
import { PosterWords } from "@/components/rows/poster-words";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { TitlePoster } from "@/components/title-poster";
import { api } from "@/lib/api";
import { audienceSummary, rowOverrides } from "@/lib/collections";
import { DEFAULT_ROW_SLUG } from "@/lib/constants";
import { describeCron } from "@/lib/cron";
import { renderRowName, sampleLibraryName, settingString } from "@/lib/format";
import { seasonStatusLine } from "@/lib/seasons";
import { useCollectionEffectiveness, useLibraries, useSettings } from "@/lib/queries";
import type { Collection, PlexLibrary, User } from "@/lib/types";
import { cn } from "@/lib/utils";

/** Whether this name renders any chips (`RowName`), so a caller can explain what a chip IS only when
 *  one is on screen. Exact tokens only (`lib/placeholders.ts`): anything else must stay plain text,
 *  since hiding a typo like "{Library_Name}" as a chip would dress up braces Plex will print. */
export function hasRowNameToken(name: string): boolean {
  return PLACEHOLDER_SPLIT.test(name);
}

/** An AI row's name with its fixed theme filled in; any other row's name as it is. */
function cardName(collection: Collection): string {
  if (!collection.theme_name) return collection.name;
  return collection.name
    .replaceAll(THEME_EMOJI, collection.theme_emoji ?? "")
    .replaceAll(THEME, collection.theme_name)
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * The row's configured poster when it can be rendered locally or is already cached, otherwise four
 * latest picks, then a neutral tile. Seeing the configured poster here must never create artwork.
 *
 * The slot is the same size whichever it shows, so every name in the list starts at the same x. The
 * picks are `preview_titles`, from the row's most recent delivery; fewer than four would leave holes
 * in the grid, so they are the useful fallback when no configured poster is available.
 */
export function RowCollage({ collection, libraries = [] }: { collection: Collection; libraries?: PlexLibrary[] }) {
  const slot = "h-16 w-11 shrink-0 overflow-hidden rounded border sm:h-20 sm:w-14";
  const picks = collection.preview_titles;
  const poster = collection.poster;
  const version = poster
    ? [poster.mode, poster.title, poster.subtitle, poster.style].join("|")
    : "";
  const [imageFailedFor, setImageFailedFor] = useState<string | null>(null);
  const libraryName = representativeLibraryName(collection, libraries);
  const season = collection.season_status?.showing ?? collection.season_status?.next ?? undefined;
  const theme = collection.theme_name
    ? { name: collection.theme_name, emoji: collection.theme_emoji ?? "" }
    : undefined;

  if (poster?.mode === "text") {
    const rowName = renderRowName(
      collection.name_template || collection.name,
      "Fargo",
      "Sarah",
      libraryName,
      season,
      theme,
    );
    const title = renderRowName(poster.title, "Fargo", "Sarah", libraryName, season, theme);
    const subtitle = renderRowName(poster.subtitle, "Fargo", "Sarah", libraryName, season, theme);
    return (
      <div aria-hidden="true" className={slot}>
        <PosterWords
          title={title || rowName || "Picked for You"}
          subtitle={subtitle}
          posterStyle={poster.style}
        />
      </div>
    );
  }
  if (poster?.has_image && imageFailedFor !== version) {
    return (
      <img
        // The API only serves existing upload/AI bytes here. It never triggers image generation.
        src={`${api.posterImageUrl(collection.id)}?v=${encodeURIComponent(version)}`}
        alt=""
        aria-hidden="true"
        className={cn(slot, "object-cover")}
        onError={() => setImageFailedFor(version)}
      />
    );
  }
  if (picks.length >= 4) {
    return (
      <div aria-hidden="true" className={cn(slot, "grid grid-cols-2 grid-rows-2 gap-px bg-border")}>
        {picks.slice(0, 4).map((pick) => (
          <TitlePoster key={pick.rating_key} ratingKey={pick.rating_key} className="size-full rounded-none border-0 sm:size-full" />
        ))}
      </div>
    );
  }
  return (
    <div
      aria-hidden="true"
      title="No poster — Plex uses its own artwork for this row"
      className={cn(slot, "flex items-center justify-center border-dashed bg-muted/40")}
    >
      <ImageIcon className="size-4 text-muted-foreground/60" />
    </div>
  );
}

/** One card cannot promise a different poster for every library, so use its configured library first. */
function representativeLibraryName(
  collection: Collection,
  libraries: PlexLibrary[],
): string {
  const configured = libraries.find((library) => collection.library_keys.includes(library.key));
  const compatible = libraries.find((library) => collection.media === "both" || library.type === collection.media);
  return configured?.title ?? compatible?.title ?? sampleLibraryName(collection.media);
}

/** When the row runs, in words; a row with no schedule only runs when someone presses Run now. */
function scheduleWords(schedule: string | null): string {
  if (!schedule?.trim()) return "Manual runs only";
  return describeCron(schedule) || "Custom schedule";
}

/** "Last built 02:30 today · 4 people", or what stands in for it when there is no build to name. */
function builtLine(
  collection: Collection,
  reach: number | null,
  /** undefined while the row's history is loading or failed to load: then nothing is claimed about it. */
  lastBuilt: string | null | undefined,
): { text: string; tone: "ok" | "off" | "none" } {
  const last = lastBuilt ? builtAt(lastBuilt) : null;
  if (!collection.enabled) {
    return { text: `Off, not on anyone's Plex${last ? ` · last built ${last}` : ""}`, tone: "off" };
  }
  const people =
    reach === null ? null : collection.build === "shared" ? `one copy for ${peopleCount(reach)}` : peopleCount(reach);
  const built = last ? `Last built ${last}` : lastBuilt === null ? "Not built yet" : null;
  const parts = [built, people].filter(Boolean);
  return { text: parts.join(" · "), tone: last ? "ok" : "none" };
}

/**
 * One row in the Rows list: what it is, who it reaches, when it last built, and the two things done
 * to a row most often (switch it, run it). Everything else is in the "⋯" menu.
 *
 * The menu holds the way out too. A red "Remove or delete" on every card put red text on the page
 * before anyone had decided to remove anything, and the editor's Danger zone is where the difference
 * between the two is written down — so the menu item goes there rather than acting itself.
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
  const history = useCollectionEffectiveness(collection.id);
  const isDefault = collection.slug === DEFAULT_ROW_SLUG;
  // null until the library list actually arrives — a half-loaded card must not label a row's
  // libraries with raw Plex section keys, which mean nothing to the owner.
  const overrides = rowOverrides(collection, libraries.isSuccess ? libraries.data : null, settings.data);

  // The default row's size is delivered from Settings → Defaults, not its own column (which the
  // backend ignores). Show the effective value so the card can't advertise a size no user gets.
  const globalSize = Number(settingString(settings.data ?? {}, "row.size"));
  const effectiveSize = isDefault && Number.isFinite(globalSize) && globalSize > 0 ? globalSize : collection.size;
  const built = builtLine(collection, rowReach(collection, users), history.data?.last_delivered_at);

  return (
    <Card>
      <CardContent className="flex flex-wrap items-center gap-4 p-4">
        <div className={cn(!collection.enabled && "opacity-50")}>
          <RowCollage collection={collection} libraries={libraries.isSuccess ? libraries.data : []} />
        </div>
        <div className="min-w-0 flex-1 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 className="min-w-0 max-w-full text-base font-medium [overflow-wrap:anywhere]">
              <Link
                to={`/rows/${collection.id}`}
                className="rounded-sm hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
              >
                <RowName name={cardName(collection)} className="" />
              </Link>
            </h2>
            {collection.theme_id !== null && collection.theme_id !== undefined && (
              <Badge>
                <Sparkles className="size-3" aria-hidden="true" />
                AI
              </Badge>
            )}
            {isDefault && <Badge variant="outline">default</Badge>}
            {/* A seasonal row says which season it is in, or when it comes back — "my row vanished"
                is the support question a season or a day schedule creates, answered with the date.
                The server resolves it, on the clock Plex follows. */}
            {(collection.seasons ?? []).length > 0 && seasonStatusLine(collection.season_status) && (
              <Badge variant={collection.season_status?.showing ? "secondary" : "outline"}>
                {seasonStatusLine(collection.season_status)}
              </Badge>
            )}
            {(collection.show_days ?? []).length > 0 &&
              (collection.shown_today ? (
                <Badge variant="secondary">Showing today</Badge>
              ) : (
                <Badge variant="outline">Hidden today</Badge>
              ))}
          </div>
          <p className="text-sm text-muted-foreground">
            {rowKindTitle(collection, settings.data)} · {audienceSummary(collection, users)} · {effectiveSize} titles ·{" "}
            {mediaLabel(collection.media)} · {scheduleWords(collection.schedule)}
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
          {built.text && (
            <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
              <span
                aria-hidden="true"
                className={cn(
                  "size-1.5 shrink-0 rounded-full",
                  built.tone === "ok" ? "bg-success" : "bg-faint-foreground",
                )}
              />
              {built.text}
            </p>
          )}
        </div>
        {/* Wraps under the text on a phone: the switch, Run now and the menu need more than a 320px
            line beside a poster and a name. */}
        <div className="flex w-full items-center justify-end gap-2 border-t pt-3 sm:w-auto sm:border-0 sm:pt-0">
          <RowEnableToggle collection={collection} showLabel="short" />
          <RowRunAction collection={collection} />
          <OverflowMenu
            label={`More actions for ${collection.name}`}
            items={[
              { label: "Edit", icon: Pencil, onSelect: onEdit },
              { label: "Runs", icon: ListChecks, to: `/runs?row=${encodeURIComponent(collection.slug)}` },
              // No state: the rename screen asks for the new name itself when nobody typed one.
              { label: "Rename on Plex…", icon: TextCursorInput, to: `/rows/${collection.id}/rename` },
              {
                label: "Remove or delete…",
                icon: Trash2,
                to: `/rows/${collection.id}#remove-this-row`,
                danger: true,
                separated: true,
              },
            ]}
          />
        </div>
      </CardContent>
    </Card>
  );
}
