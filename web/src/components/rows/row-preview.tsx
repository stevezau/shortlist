import type { ReactNode } from "react";

import { asColdStart } from "@/lib/cold-start";
import { describeCron } from "@/lib/cron";
import { SEASON_TOKENS, TOP_SEED } from "@/lib/placeholders";
import { targetsLibrary } from "@/lib/placement";
import { asRatingSource, RATING_LABELS } from "@/lib/rating-sources";
import { requestsSummary } from "@/lib/requests";
import { seasonTiming } from "@/lib/seasons";
import {
  basedOn,
  effectiveMaxSeeds,
  FILL_META,
  followsAWatch,
  hiddenButRead,
  KIND_META,
  namesASeed,
  rowKindOf,
  takeTurnsEnabled,
  visibleSettings,
  type RowFill,
  type RowKind,
  type RowKindContext,
  type RowSettingKey,
} from "@/lib/row-kinds";
import { recencyGlobalValue, refreshDaysGlobalValue, watchedPctGlobalValue } from "@/lib/row-globals";
import { showDaysSummary } from "@/lib/show-days";
import { sourceShortLabel } from "@/lib/sources";
import type { CollectionInput, HubAnchorMap, PlexLibrary, Season, Settings, User } from "@/lib/types";

type PreviewContext = Pick<RowKindContext, "isDefault" | "globalMaxSeeds" | "defaultRowName" | "globalSources">;

/** One line of the panel, and the settings it reports (`data-fact`). */
type FactLine = { label: string; keys: RowSettingKey[]; value: ReactNode };

const FOLLOWING_GLOBAL = "Following the global default";

function globalNumber(settings: Settings | undefined, key: string): number | null {
  const raw = settings?.[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : null;
}

/** Marks a value the row inherits. The value beside it is always the global's real one. */
function Inherited({ when }: { when: boolean }) {
  return when ? <span className="text-muted-foreground"> (global default)</span> : null;
}

/** A second sentence under a line's value. */
function Detail({ children }: { children: ReactNode }) {
  return <span className="block text-muted-foreground">{children}</span>;
}

function capitalised(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

function watches(count: number): string {
  return `${count} watch${count === 1 ? "" : "es"}`;
}

/** "1 day", "30 days". */
function daysLabel(days: number): string {
  return `${days} day${days === 1 ? "" : "s"}`;
}

/** How often the row swaps titles, in words, from the day count the engine reads.
 *
 * 0 is never, 1 is nightly, N is every N days — the stored number IS the cadence, so there is no
 * conversion to mirror here any more. It used to be a 0..1 fraction and this function carried its
 * own copy of `round(1 + (1 - f) * 13)`, one of four uncoordinated copies of that curve; saying it
 * in days is the whole point, because "50% fresh" told nobody when their row would change.
 */
function refreshWords(days: number | null): string {
  if (days === null) return FOLLOWING_GLOBAL;
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

/** One library's shelf position, as the shelf-position control reads the same entry. */
function shelfWords(
  entry: HubAnchorMap[string] | undefined,
  rowNames: Record<string, string>,
): string {
  if (entry?.enabled === false) return "not placed";
  // A Shortlist row is stored by slug; the owner knows it by its name.
  const anchor = (entry?.row ? rowNames[entry.row] || entry.row : entry?.anchor || "").trim();
  // No entry, Top, or a mode picked with no anchor yet (Save drops that one): the top.
  if (!entry || entry.top || !anchor) return "top";
  return `right ${entry.before ? "before" : "after"} “${anchor}”`;
}

/** Where the row sits on each library's Recommended shelf. */
function shelfLine(
  input: CollectionInput,
  libraries: PlexLibrary[],
  rowNames: Record<string, string>,
): string {
  const targeted = libraries.filter((library) =>
    targetsLibrary(library, input.library_keys, input.media),
  );
  // Before the libraries load, only the libraries with a choice of their own can be named.
  const places =
    targeted.length > 0
      ? targeted.map((library) => [library.title, shelfWords(input.hub_anchor[library.key], rowNames)])
      : Object.entries(input.hub_anchor).map(([key, entry]) => [`Library ${key}`, shelfWords(entry, rowNames)]);
  if (places.every(([, words]) => words === "top")) return "Top of the Recommended shelf";
  return places.map(([name, words]) => `${name}: ${words}`).join(" · ");
}

const ORDER_WORDS: Record<string, string> = {
  best: "Best match first",
  rating: "Highest rated first",
  newest: "Newest released first",
  shuffle: "Shuffled daily",
  new_first: "Just-added titles first",
  rotate: "Taking turns at the front",
};

function orderWords(input: CollectionInput, settings: Settings | undefined): string {
  // A shared row is ranked by how many people watched each title, which is its "best match".
  const words =
    input.build === "shared" && input.pick_order === "best"
      ? "Most watched first"
      : (ORDER_WORDS[input.pick_order] ?? "Best match first");
  if (input.pick_order !== "rating") return words;
  const service = RATING_LABELS[asRatingSource(settings?.["recommendations.rating_source"])];
  return `${words}, by ${service} score`;
}

function kindValue(kind: RowKind, fill: RowFill): ReactNode {
  const title =
    kind === "seasonal" ? `Seasonal, filled like ${FILL_META[fill].title}` : KIND_META[kind].title;
  return (
    <>
      {title}
      {/* A shared row is a straight tally: it shows what people HAVE watched, pools everyone's
          viewing, and searches nothing. Saying so here is why no sources line follows. */}
      {fill === "popular" && (
        <>
          <Detail>What people here have watched most</Detail>
          <Detail>Everyone&apos;s viewing, pooled — no search, no AI</Detail>
        </>
      )}
      {fill === "again" && <Detail>Things they&apos;ve already finished, first</Detail>}
      {fill === "requests" && <Detail>Only what they asked for — no recommendations</Detail>}
    </>
  );
}

/** Which of their requests a Your requests row shows: the arrival window, and any own tags it reads. */
function requestsRowValue(input: CollectionInput): ReactNode {
  const days = input.requests_window_days;
  const pattern = input.requests_tag_pattern.trim();
  return (
    <>
      {days > 0
        ? `What they asked for that landed in the last ${daysLabel(days)}, newest first`
        : "Everything they asked for that's on Plex, newest first"}
      <Detail>Each title leaves once they&apos;ve watched it</Detail>
      <Detail>
        {pattern
          ? `Read from Overseerr's requests and your own tags, ${pattern}`
          : "Read from Overseerr's requests, or Radarr/Sonarr request tags"}
      </Detail>
    </>
  );
}

/** What the row does for someone with too little history, and what it's called for them. */
function someoneNewValue(
  input: CollectionInput,
  settings: Settings | undefined,
  { coldStartShown, seedName }: { coldStartShown: boolean; seedName: boolean },
): ReactNode {
  const minHistory = globalNumber(settings, "recommendations.min_history");
  const until =
    minHistory === null ? "until they've watched enough" : `until they've watched ${minHistory} titles`;
  const rawGlobal = settings?.["recommendations.cold_start"];
  const globalColdStart = typeof rawGlobal === "string" ? asColdStart(rawGlobal) : null;
  // Where the row's kind has no cold-start choice, only the name decides.
  const coldStart = coldStartShown ? (input.cold_start ?? globalColdStart) : "popular";
  if (coldStart === null) return FOLLOWING_GLOBAL;

  const fallback = input.fallback_name.trim();
  // A name that follows a watch has nothing to name the row after, so only the fallback can — and
  // without one there is no row whatever the cold-start choice says.
  if (coldStart === "popular" && seedName && !fallback) {
    return `No row ${until}: there's no watch to name it after`;
  }
  // `delivery.render_row_name` drops a fallback that itself needs a watch or a season, so it names
  // nobody's row either.
  const unfillable = [TOP_SEED, ...SEASON_TOKENS].find((token) => fallback.includes(token));
  if (coldStart === "popular" && seedName && unfillable) {
    return `No row ${until}: the name for someone new uses ${unfillable}, which can't be filled in for them either`;
  }
  let words: string;
  if (coldStart === "skip") words = `No row ${until}`;
  else if (seedName) words = `The server's highest-rated titles, named “${fallback}”, ${until}`;
  else words = `The server's highest-rated titles, ${until}`;

  return (
    <>
      {words}
      <Inherited when={coldStartShown && input.cold_start === null} />
    </>
  );
}

/** Take turns, including a rotation the kind hides but the engine still applies (`hiddenButRead`). */
function takeTurnsValue(input: CollectionInput, usable: boolean, fill: RowFill): string {
  const window = input.seed_window;
  if (window <= 1) return usable ? "No — always their latest" : "No — not with a blend of 3 or more";
  if (usable) return `Between their last ${watches(window)}, a different one each night`;
  const why =
    fill === "byw"
      ? "a blend of 3 or more can't use it"
      : fill === "again"
        ? "its new picks only take turns while they match 1 or 2 watches"
        : `${FILL_META[fill].title} doesn't use it`;
  return `Still between their last ${watches(window)}, which rebuilds it every night (${why})`;
}

function rebuildsValue(schedule: string): string {
  const cron = schedule.trim();
  if (!cron) return "Off — only when you run it by hand";
  return describeCron(cron) || `On its own schedule (${cron})`;
}

/**
 * Every line of the panel, in the editor's order. Exactly one line per setting the editor shows for
 * this row's kind (`visibleSettings` + `hiddenButRead`), bar `NO_FACT_LINE`, and none for a setting
 * it hides.
 *
 * A setting at a value that changes nothing (the row is on, every day, no sort prefix, started
 * series allowed) has no line of its own; its key rides on the line whose sentence already covers
 * it. A line with no keys reports what no setting on this row controls: the default row's size
 * (Settings), the nightly cadence the engine forces, a shared row's requests.
 */
function rowFacts({
  input,
  ctx,
  enabled,
  users,
  libraries,
  settings,
  seasons,
  rowNames,
}: {
  input: CollectionInput;
  ctx: PreviewContext;
  enabled: boolean;
  users: User[];
  libraries: PlexLibrary[];
  settings: Settings | undefined;
  seasons: Season[];
  rowNames: Record<string, string>;
}): FactLine[] {
  const shown = visibleSettings(input, ctx);
  const hidden = hiddenButRead(input, ctx);
  const { kind, fill } = rowKindOf(input, ctx);
  const seedName = namesASeed(input, ctx);
  const seeds = effectiveMaxSeeds(input, ctx);
  const facts: FactLine[] = [];
  const add = (label: string, keys: (RowSettingKey | false)[], value: ReactNode) =>
    facts.push({ label, keys: keys.filter((key): key is RowSettingKey => key !== false), value });

  if (!enabled) {
    add(
      "Status",
      ["enabled"],
      // Saving `enabled: false` removes its collections there and then (`row_changes.py`, RECONCILE
      // collection.disable), and runs skip a row that is off.
      "Off — switching it off takes it off Plex straight away, and nothing is built until you turn it back on.",
    );
  }
  add("What it is", ["kind"], kindValue(kind, fill));
  add("Who gets it", ["audience"], whoSeesIt(input, users));
  if (shown.has("min_watchers")) {
    add("Counts", ["min_watchers"], `Only titles at least ${input.min_watchers} people here have watched`);
  }
  if (shown.has("seasons")) {
    const span = (lead: number, after: number) =>
      `${daysLabel(lead)} before to ${after > 0 ? `${daysLabel(after)} after` : "the day itself"}`;
    // A built-in follows the row's timing; a season the owner made carries its own (#137 D8). Seasons
    // that show for the same span share one clause.
    const bySpan = new Map<string, string[]>();
    for (const season of seasons.filter((s) => input.seasons.includes(s.slug))) {
      const { lead, after } = seasonTiming(season, input.season_lead_days, input.season_after_days);
      const key = span(lead, after);
      bySpan.set(key, [...(bySpan.get(key) ?? []), `${season.emoji} ${season.name}`]);
    }
    const count = input.seasons.length;
    const clauses =
      bySpan.size > 0
        ? [...bySpan].map(([when, names]) => `${names.join(", ")} — ${when}`).join("; ")
        : `${count} season${count === 1 ? "" : "s"} — ${span(input.season_lead_days, input.season_after_days)}`;
    add("Seasons", ["seasons"], `${clauses}, hidden between seasons`);
  }
  if (shown.has("requests_window_days")) {
    add(
      "Which requests",
      ["requests_window_days", "requests_sources", "requests_tag_pattern"],
      requestsRowValue(input),
    );
  }
  if (shown.has("based_on")) {
    add(
      "Based on",
      ["based_on"],
      <>
        {capitalised(basedOn(seeds, input.media))}
        <Inherited when={input.max_seeds === null} />
      </>,
    );
  }
  if (shown.has("seed_window") || hidden.includes("seed_window")) {
    const usable = shown.has("seed_window") && takeTurnsEnabled(input, ctx);
    add("Takes turns", ["seed_window"], takeTurnsValue(input, usable, fill));
  }
  if (shown.has("rewatch_cooldown_days")) {
    const days = input.rewatch_cooldown_days;
    add(
      "Skips",
      ["rewatch_cooldown_days"],
      days > 0
        ? `Anything they finished in the last ${daysLabel(days)}`
        : "Nothing — anything they've finished can come back",
    );
  }
  if (shown.has("max_seeds")) {
    // On Watch it again the count only finds the new picks that fill the row once rewatches run out.
    add(
      fill === "again" ? "When they run out" : "Built from",
      ["max_seeds"],
      <>
        {fill === "again"
          ? `New picks, matched to their last ${watches(seeds)}`
          : `Their last ${watches(seeds)}, blended`}
        <Inherited when={input.max_seeds === null} />
      </>,
    );
  }
  if (shown.has("cold_start") || shown.has("fallback_name")) {
    add(
      "Someone new",
      [shown.has("cold_start") && "cold_start", shown.has("fallback_name") && "fallback_name"],
      someoneNewValue(input, settings, { coldStartShown: shown.has("cold_start"), seedName }),
    );
  }

  add("Built in", ["libraries"], librariesLine(input, libraries));
  // The default row is built to the global `row.size`, whatever its own column holds.
  const size = ctx.isDefault ? globalNumber(settings, "row.size") : input.size;
  add(
    "How many",
    [shown.has("size") && "size"],
    size === null ? (
      FOLLOWING_GLOBAL
    ) : (
      <>
        {`Up to ${size} title${size === 1 ? "" : "s"}`}
        <Inherited when={ctx.isDefault} />
      </>
    ),
  );
  if (shown.has("candidate_sources")) {
    const inheriting = input.candidate_sources.length === 0;
    const seasonal = input.seasons.length > 0;
    const sources = (inheriting ? ctx.globalSources : input.candidate_sources).filter(
      // A seasonal row never runs AI web search (`effective_row_sources`), so the panel must not list it.
      (source) => !seasonal || source !== "llm_web",
    );
    add(
      "Found via",
      ["candidate_sources"],
      <>
        {/* A seasonal row's own titles come first: they are what the row is made of. */}
        {[...(seasonal ? ["Seasonal list"] : []), ...sources.map(sourceShortLabel)].join(", ")}
        <Inherited when={inheriting} />
        {/* The engine keeps only the season's titles from every source (`rows.py`). */}
        {seasonal && <Detail>Only titles on that season&apos;s list are kept.</Detail>}
      </>,
    );
  }
  if (shown.has("recent_count")) {
    const count = input.recent_count ?? globalNumber(settings, "recommendations.recent_count");
    add(
      "AI web search",
      ["recent_count"],
      count === null ? (
        FOLLOWING_GLOBAL
      ) : (
        <>
          {`Asks about their last ${watches(count)}`}
          <Inherited when={input.recent_count === null} />
        </>
      ),
    );
  }
  // "Only series they haven't started" gets its own line when it's on; off, it changes nothing the
  // already-watched line doesn't already say.
  const unstartedShown = shown.has("unstarted_only");
  const seriesLine = unstartedShown && (input.unstarted_only || !shown.has("watched_pct"));
  if (shown.has("watched_pct")) {
    const pct = input.watched_pct ?? watchedPctGlobalValue(settings);
    add(
      "Contents",
      ["watched_pct", unstartedShown && !seriesLine && "unstarted_only"],
      pct === null ? (
        FOLLOWING_GLOBAL
      ) : (
        <>
          {pct <= 0
            ? "Only things they haven't seen"
            : `Up to ${Math.round(pct * 100)}% things they've already seen`}
          <Inherited when={input.watched_pct === null} />
        </>
      ),
    );
  }
  if (seriesLine) {
    add(
      "Series",
      ["unstarted_only"],
      input.unstarted_only ? "Only series they have never started" : "Any series, started or not",
    );
  }
  if (shown.has("recency")) {
    const recency = input.recency ?? recencyGlobalValue(settings);
    const pct = recency === null ? null : Math.round(recency * 100);
    add(
      "Recent releases",
      ["recency"],
      pct === null ? (
        FOLLOWING_GLOBAL
      ) : (
        <>
          {pct <= 0
            ? "Release date doesn't count"
            : pct >= 100
              ? "Strongly favours new releases"
              : `Leans ${pct}% towards recent releases`}
          <Inherited when={input.recency === null} />
        </>
      ),
    );
  }
  if (shown.has("pick_order")) {
    add("Order", ["pick_order", shown.has("rated_by") && "rated_by"], orderWords(input, settings));
  } else if (fill === "requests") {
    add("Order", [], "Newest arrival first, always");
  }

  add("Rebuilds", ["schedule", enabled && "enabled"], rebuildsValue(input.schedule));
  if (shown.has("refresh_days")) {
    add(
      "Changes",
      ["refresh_days"],
      <>
        {refreshWords(input.refresh_days ?? refreshDaysGlobalValue(settings))}
        <Inherited when={input.refresh_days === null && refreshDaysGlobalValue(settings) !== null} />
      </>,
    );
  } else if (followsAWatch(input, ctx) && fill !== "popular") {
    // `effective_refresh_days` forces nightly, whatever the row stores — so the stored value gets no line.
    add(
      "Changes",
      [],
      <>
        Every night
        <span className="text-muted-foreground">
          {" — forced, because "}
          {seedName
            ? "its name follows their latest watch"
            : `it takes turns between their last ${watches(input.seed_window)}`}
        </span>
      </>,
    );
  }
  if (shown.has("idle_hold_days")) {
    const hold = input.idle_hold_days ?? globalNumber(settings, "recommendations.idle_hold_days");
    add(
      "Not watching",
      ["idle_hold_days"],
      hold === null ? (
        FOLLOWING_GLOBAL
      ) : (
        <>
          {hold <= 0
            ? "No hold — it changes whether or not they're watching"
            : `Waits up to ${daysLabel(hold)} for them to watch something new`}
          <Inherited when={input.idle_hold_days === null} />
        </>
      ),
    );
  }

  const everyDay = showDaysSummary(input.show_days) === "Every day";
  const prefix = input.sort_title_prefix.trim();
  add(
    "Appears on",
    ["placement", everyDay && "show_days", !prefix && "sort_title_prefix"],
    `${whereItShows(input)}${everyDay ? ", every day" : ""}`,
  );
  // Only for a row that actually narrows its days. Without this the panel says "Home and the
  // library" and stops, which is true but not the whole answer for a row people only see on Fridays.
  if (!everyDay) add("Only on", ["show_days"], showDaysSummary(input.show_days));
  add("Shelf position", ["hub_anchor"], shelfLine(input, libraries, rowNames));
  if (prefix) {
    add("Sort prefix", ["sort_title_prefix"], `“${prefix}” goes before its name when Plex sorts collections`);
  }

  add(
    "Requests",
    [shown.has("requests") && "requests"],
    requestsSummary(input, settings, { shared: !shown.has("requests") }),
  );

  return facts;
}

function Fact({ label, keys, value }: FactLine) {
  return (
    <div
      data-fact={keys.length > 0 ? keys.join(" ") : undefined}
      className="flex gap-3 py-2 text-sm"
    >
      <dt className="w-28 shrink-0 text-muted-foreground">{label}</dt>
      <dd className="min-w-0 flex-1 [overflow-wrap:anywhere]">{value}</dd>
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
 * Every setting the editor shows for the row's kind has exactly one line, marked `data-fact` with
 * the settings it reports, driven by the same kind→settings map the editor uses (design §8). The
 * exceptions are `NO_FACT_LINE`: the name, description and poster are shown by `RowPlexCard`, beside
 * the fields that set them.
 *
 * Deliberately not a Plex mock-up. A fake shelf of grey boxes would imply we know which titles land
 * in it, and we do not until the row runs.
 */
export function RowPreview({
  input,
  ctx,
  enabled = input.enabled,
  users,
  libraries,
  settings,
  seasons = [],
  rowNames = {},
  compact = false,
}: {
  input: CollectionInput;
  /** The globals the row's kind is read against — the editor's own, so the two cannot disagree. */
  ctx: PreviewContext;
  /** Whether the row is on. The header's switch saves that straight away, so the saved row is the
   *  truth rather than the form. */
  enabled?: boolean;
  users: User[];
  libraries: PlexLibrary[];
  settings: Settings | undefined;
  /** The season catalogue, to name the seasons the row follows; empty while it loads. */
  seasons?: Season[];
  /** Every row's name by slug, to name a row this one is placed beside. */
  rowNames?: Record<string, string>;
  compact?: boolean;
}) {
  const facts = rowFacts({ input, ctx, enabled, users, libraries, settings, seasons, rowNames });
  const primaryLabels = new Set([
    "Status", "Who gets it", "How many", "Based on", "Built from", "Counts",
    "Which requests", "When they run out", "Found via", "Contents", "Skips",
    "Rebuilds", "Appears on", "Only on", "Seasons", "Shelf position",
  ]);
  const primary = compact ? facts.filter((fact) => primaryLabels.has(fact.label)) : facts;
  const secondary = compact ? facts.filter((fact) => !primaryLabels.has(fact.label)) : [];

  // The heading lives in the PAGE, above this card, not inside it — so it lines up with "Row
  // settings" over the left column and both columns start at the same y. A heading inside the card
  // sat a card's padding lower than the one beside it, which read as two unrelated things.
  return (
    <div aria-label="Outcome facts" className="space-y-3 rounded-lg border bg-card p-4 [&_dt]:w-20 [&_dd]:text-xs [&_dt]:text-xs">
      <dl className="divide-y">
        {primary.map((fact) => (
          <Fact key={fact.label} {...fact} />
        ))}
      </dl>
      {secondary.length > 0 && <details className="border-t pt-3"><summary className="cursor-pointer text-xs text-muted-foreground">All outcome details</summary><dl className="mt-2 divide-y">{secondary.map((fact) => <Fact key={fact.label} {...fact} />)}</dl></details>}
    </div>
  );
}
