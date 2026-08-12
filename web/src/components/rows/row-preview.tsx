import type { ReactNode } from "react";

import { effectiveSources } from "@/components/rows/row-sources-field";
import { renderRowName } from "@/lib/format";
import { sourceShortLabel } from "@/lib/sources";
import type { CollectionInput, PlexLibrary, Settings, User } from "@/lib/types";

/** How often the row swaps titles, in words, from the day count the engine reads.
 *
 * 0 is never, 1 is nightly, N is every N days — the stored number IS the cadence, so there is no
 * conversion to mirror here any more. It used to be a 0..1 fraction and this function carried its
 * own copy of `round(1 + (1 - f) * 13)`, one of four uncoordinated copies of that curve; saying it
 * in days is the whole point, because "50% fresh" told nobody when their row would change.
 */
function updateFrequency(
  input: CollectionInput,
  followsAWatch: boolean,
  globalRefreshDays: number | null,
): string {
  if (followsAWatch) return "Every night";
  // An inheriting row is resolved against the real global, not reported as "the default" — the panel
  // exists to answer "what will this row do", and naming the setting instead of its effect is the
  // one answer it must not give. Only unresolvable while settings are still loading.
  const days = input.refresh_days ?? globalRefreshDays;
  if (days === null) return "Following the global default";
  if (days <= 0) return "Never — built once, then left alone";
  if (days === 1) return "Every night";
  if (days === 7) return "Once a week";
  return `Every ${days} days`;
}

/** Where the row turns up, in the words someone would use about their own Plex. */
function whereItShows(input: CollectionInput): string {
  const names: Record<string, string> = {
    both: "Home and the library",
    home: "Home only",
    library: "The library only",
    off: "Nowhere — the Collections tab only",
  };
  const mine = names[input.placement] ?? "Home and the library";
  const theirs = names[input.placement_friends] ?? "Home and the library";
  if (input.build === "shared" || mine === theirs) return mine;
  return `You: ${mine.toLowerCase()} · Everyone else: ${theirs.toLowerCase()}`;
}

/** Who ends up with this row, counting only people a run will actually build for. */
function whoSeesIt(input: CollectionInput, users: User[]): string {
  const active = users.filter((u) => u.enabled && !u.prefs?.paused);
  const reach =
    input.audience === "everyone"
      ? active
      : active.filter((u) => input.audience_user_ids.includes(u.id));
  const count = reach.length ? ` (${reach.length})` : "";
  if (input.build === "shared") {
    return input.audience === "everyone"
      ? `One row, shared with everyone${count}`
      : `One row, shared with the people you picked${count}`;
  }
  return input.audience === "everyone"
    ? `Their own copy, for everyone${count}`
    : `Their own copy, for the people you picked${count}`;
}

/** The libraries this row builds a collection in — by name where they are known.
 *
 * `[]` means "every library of this row's media type", which is a different statement from a list
 * and has to be said as one, or a row covering four libraries reads identically to one covering the
 * two you happened to tick.
 */
function librariesLine(
  input: CollectionInput,
  libraries: PlexLibrary[],
): string {
  if (input.library_keys.length === 0) {
    return (
      {
        movie: "Every movie library",
        show: "Every TV library",
        both: "Every movie and TV library",
      }[input.media] ?? "Every library"
    );
  }
  const named = input.library_keys
    .map((key) => libraries.find((l) => String(l.key) === String(key))?.title)
    .filter(Boolean);
  return named.length
    ? named.join(", ")
    : `${input.library_keys.length} librar${input.library_keys.length === 1 ? "y" : "ies"}`;
}

/** What the row is allowed to contain, watched-wise. The rewatch switch INVERTS the cap rather than
 *  raising it, so it has to be read first or the sentence contradicts the row. */
function watchedLine(
  input: CollectionInput,
  globalWatchedPct: number | null,
): string {
  if (input.rewatch) return "Things they've already finished, first";
  if (input.unstarted_only) return "Only series they have never started";
  const pct = input.watched_pct ?? globalWatchedPct;
  if (pct === null) return "Following the global default";
  if (pct <= 0) return "Only things they haven't seen";
  return `Up to ${Math.round(pct * 100)}% things they've already seen`;
}

/** What decides the row's contents: how many watches seed it, and whether it cycles between them. */
function builtFromLine(input: CollectionInput): string | null {
  const n = input.max_seeds;
  if (n === null) return "Their recent viewing, blended";
  const base = n === 1 ? "One recent watch" : `${n} recent watches`;
  if (input.seed_window > 1) {
    return `${base}, cycling through their last ${input.seed_window}`;
  }
  return n === 1 ? `${base} — their latest` : base;
}

const ORDER_WORDS: Record<string, string> = {
  best: "Best match first",
  rating: "Highest rated first",
  newest: "Newest released first",
  shuffle: "Shuffled daily",
  new_first: "Just-added titles first",
  rotate: "Taking turns at the front",
};

function Fact({ label, value }: { label: string; value: ReactNode }) {
  return (
    <div className="flex gap-3 py-2 text-sm">
      <dt className="w-28 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 flex-1">{value}</dd>
    </div>
  );
}

/**
 * What the row being edited will actually produce, in plain words, updating as the form changes.
 *
 * The settings on the left are abstractions — a cadence, a seed budget, a placement enum. This is
 * the only place someone can check the thing they actually care about: "what will Sarah see on her
 * Home screen tonight?". Every fact here is derived from the CURRENT form state rather than the
 * saved row, so it answers that question before anything is written to Plex.
 *
 * It covers every setting whose effect is not visible from its own control, which is why it is long:
 * a panel that restated four of nineteen settings left the other fifteen exactly as opaque as they
 * were, and the ones it left out (sources, libraries, the watched policy) are the ones that decide
 * what a row is.
 *
 * Deliberately not a Plex mock-up. A fake shelf of grey boxes would imply we know which titles land
 * in it, and we do not until the row runs.
 */
export function RowPreview({
  input,
  users,
  libraries,
  settings,
  followsAWatch,
  globalRefreshDays,
  globalWatchedPct,
}: {
  input: CollectionInput;
  users: User[];
  libraries: PlexLibrary[];
  settings: Settings | undefined;
  followsAWatch: boolean;
  /** The server-wide cadence, so an inheriting row still shows a real one. */
  globalRefreshDays: number | null;
  globalWatchedPct: number | null;
}) {
  const template = input.name_template || input.name;
  // The sample library has to match the row's media type, or a TV-only row previews as
  // "More Movies to watch" — a name it can never produce, on the one panel whose job is to show the
  // name it WILL produce.
  const sampleLibrary = input.media === "show" ? "TV Shows" : "Movies";
  const shown =
    renderRowName(template, "Fargo", "Sarah", sampleLibrary) ||
    "Picked for You";
  // What actually varies depends on the row. A per-person row renders a different name for each
  // person, from their own viewing; a SHARED row is one collection everybody sees, so only
  // {library_name} moves — telling someone their shared row is named per person is simply untrue.
  const perPerson = /\{(top_seed|user)\}/.test(template);
  const perLibrary = template.includes("{library_name}");
  const caption =
    input.build === "shared"
      ? perLibrary
        ? "Example only — the real library name fills in, so each library gets its own."
        : null
      : perPerson
        ? "Example only — each person gets their own name here, from their own viewing."
        : perLibrary
          ? "Example only — the real library name fills in, so each library gets its own."
          : null;

  const sources = effectiveSources(input.candidate_sources, settings);
  const builtFrom = builtFromLine(input);

  // The heading lives in the PAGE, above this card, not inside it — so it lines up with "Row
  // settings" over the left column and both columns start at the same y. A heading inside the card
  // sat a card's padding lower than the one beside it, which read as two unrelated things.
  return (
    <div className="space-y-4 rounded-lg border bg-card p-5">
      <div className="rounded-md border bg-muted/30 p-4">
        <p className="text-xs uppercase tracking-wide text-muted-foreground">
          On Plex it reads
        </p>
        <p className="mt-1 break-words text-base font-medium">“{shown}”</p>
        {caption && (
          <p className="mt-2 text-xs text-muted-foreground">{caption}</p>
        )}
      </div>

      <dl className="divide-y">
        <Fact label="Who gets it" value={whoSeesIt(input, users)} />
        <Fact label="Built in" value={librariesLine(input, libraries)} />
        <Fact
          label="How many"
          value={`Up to ${input.size} title${input.size === 1 ? "" : "s"}`}
        />
        <Fact label="Contents" value={watchedLine(input, globalWatchedPct)} />
        {builtFrom && <Fact label="Built from" value={builtFrom} />}
        <Fact
          label="Order"
          value={ORDER_WORDS[input.pick_order] ?? "Best match first"}
        />
        <Fact
          label="Found via"
          value={
            sources.length
              ? sources.map(sourceShortLabel).join(", ")
              : "The global default sources"
          }
        />
        <Fact
          label="Updates"
          value={updateFrequency(input, followsAWatch, globalRefreshDays)}
        />
        <Fact label="Appears on" value={whereItShows(input)} />
        {input.request_tag.trim() && (
          <Fact
            label="Request tag"
            value={`Tagged “${input.request_tag.trim()}”`}
          />
        )}
      </dl>
    </div>
  );
}
