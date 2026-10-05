import { useLayoutEffect, useRef } from "react";

import { api } from "@/lib/api";
import { renderRowName, sampleLibraryName } from "@/lib/format";
import { fillPlaceholders, LIBRARY_NAME, TOP_SEED, USER, usesSeason, usesTheme } from "@/lib/placeholders";
import type { CollectionInput, Season } from "@/lib/types";

/** The sample person and library every preview on this card is filled in for. */
const SAMPLE = { topSeed: "Fargo", user: "Sarah" } as const;

/**
 * Fill a description's placeholders with the sample values. Line breaks are kept: `renderRowName`
 * collapses whitespace around `{library_name}`, which is right for a one-line title and would
 * flatten a description typed over several lines — the engine's `render_description` keeps them too.
 */
function renderDescription(
  template: string,
  libraryName: string,
  season: { name: string; emoji: string } = { name: "Christmas", emoji: "🎄" },
  theme?: { name: string; emoji: string },
): string {
  return fillPlaceholders(template, { ...SAMPLE, libraryName, season, theme }).trim();
}

/** What varies about the name, in the words the caption uses — or null when nothing does. */
function nameCaption(input: CollectionInput, template: string): string | null {
  // A per-person row renders a different name for each person, from their own viewing; a SHARED
  // row is one collection everybody sees, so only {library_name} moves — telling someone their
  // shared row is named per person is simply untrue.
  const perPerson = template.includes(TOP_SEED) || template.includes(USER);
  const perLibrary = template.includes(LIBRARY_NAME);
  if (usesSeason(template)) {
    return "Example only — the name follows the season the row is in.";
  }
  if (usesTheme(template)) {
    return "Example only — the name follows the row’s theme.";
  }
  if (input.build !== "shared" && perPerson) {
    return "Example only — each person gets their own name here, from their own viewing.";
  }
  return perLibrary
    ? "Example only — the real library name fills in, so each library gets its own."
    : null;
}

/** Fit both lines inside a 2:3 poster, including long unbroken titles and browser zoom. */
function PosterWords({ title, subtitle }: { title: string; subtitle: string }) {
  const frame = useRef<HTMLDivElement>(null);
  const words = useRef<HTMLDivElement>(null);
  useLayoutEffect(() => {
    const fit = () => {
      if (!frame.current || !words.current || !frame.current.clientWidth) return;
      const box = frame.current;
      const content = words.current;
      const padding = Math.round(box.clientWidth * 0.09);
      box.style.padding = `${padding}px`;
      let size = Math.min(20, box.clientWidth * 0.145);
      content.style.fontSize = `${size}px`;
      while (size > 4 && (content.scrollHeight > box.clientHeight - padding * 2 || content.scrollWidth > box.clientWidth - padding * 2)) {
        size -= 0.5;
        content.style.fontSize = `${size}px`;
      }
    };
    fit();
    const observer = typeof ResizeObserver !== "undefined" ? new ResizeObserver(fit) : null;
    if (frame.current) observer?.observe(frame.current);
    return () => observer?.disconnect();
  }, [title, subtitle]);
  return <div ref={frame} data-poster-words className="flex size-full items-end bg-accent p-2 text-accent-foreground">
    <div ref={words} className="w-full min-w-0 space-y-1 text-sm leading-tight [overflow-wrap:anywhere]">
      <p className="font-semibold">{title}</p>
      {subtitle && <p className="text-[0.75em] text-accent-foreground/80">{subtitle}</p>}
    </div>
  </div>;
}

/**
 * The row as Plex shows it, filled in for a sample person: its poster, its name, its description.
 *
 * Beside the fields that set those three, because that is where the typing happens — it used to sit
 * at the top of the sidebar, a column away from the name and further still from the poster and
 * description, which lived in folded groups near the bottom of the page.
 *
 * The poster is only a real image when one exists (an uploaded one). A text or AI poster is rendered
 * by the server, so this shows its words on a plain tile and the Poster field's Preview button makes
 * the real one — drawing a lookalike here would promise a picture Plex will not show.
 */
export function RowPlexCard({
  input,
  collectionId,
  hasImage,
  sampleSeason,
  sampleTheme,
  compact = false,
}: {
  input: CollectionInput;
  collectionId: number | null;
  hasImage: boolean;
  /** The first season the row follows, to fill `{season}` with a real one; undefined uses a sample. */
  sampleSeason?: Season;
  /** An AI row's theme, to fill `{theme}` with its real name; undefined uses a sample. */
  sampleTheme?: { name: string; emoji: string };
  compact?: boolean;
}) {
  const template = input.name_template || input.name;
  const sampleLibrary = sampleLibraryName(input.media);
  const shown =
    renderRowName(template, SAMPLE.topSeed, SAMPLE.user, sampleLibrary, sampleSeason, sampleTheme) ||
    "Picked for You";
  const description = renderDescription(input.description, sampleLibrary, sampleSeason, sampleTheme);
  const caption = nameCaption(input, template);
  const mode = input.poster.mode;
  const posterTitle = renderRowName(
    input.poster.title,
    SAMPLE.topSeed,
    SAMPLE.user,
    sampleLibrary,
    sampleSeason,
    sampleTheme,
  );
  const posterSubtitle = renderRowName(
    input.poster.subtitle,
    SAMPLE.topSeed,
    SAMPLE.user,
    sampleLibrary,
    sampleSeason,
    sampleTheme,
  );

  return (
    <div className="flow-root space-y-2">
      <p className="text-xs uppercase tracking-wide text-muted-foreground">
        On Plex
      </p>
      <div className={compact ? "float-left mr-3 aspect-[2/3] w-28 max-w-full overflow-hidden rounded-md border bg-muted" : "aspect-[2/3] w-full max-w-44 overflow-hidden rounded-md border bg-muted"}>
        {mode === "upload" && collectionId !== null && hasImage ? (
          <img
            src={api.posterImageUrl(collectionId)}
            alt="This row's poster"
            className="size-full object-cover"
          />
        ) : mode === "" || mode === "upload" ? (
          <div className="flex size-full items-center justify-center p-3 text-center text-xs text-muted-foreground">
            {mode === "upload"
              ? "No image uploaded yet"
              : "Plex’s own artwork"}
          </div>
        ) : (
          <PosterWords title={posterTitle || shown} subtitle={posterSubtitle} />
        )}
      </div>
      <p className="break-words text-sm font-medium">“{shown}”</p>
      {description && (
        <p className="whitespace-pre-line break-words text-xs text-muted-foreground">
          {description}
        </p>
      )}
      {caption && <p className="text-xs text-muted-foreground">{caption}</p>}
    </div>
  );
}
