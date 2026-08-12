import type {
  RunDetail,
  RunLibraryBreakdown,
  RunSharedRowResult,
} from "@/lib/types";

/** What a run decided about one row FOR ONE PERSON, before anything was built.
 *
 *  `due` is intent, not outcome — it says the run meant to build the row, and the person's own
 *  `status` says what became of it. A run recorded before this existed has none of these, which is
 *  `null` here and must read as "not recorded", never as "no rows were considered". */
export type RowDecision = "due" | "not_due" | "muted" | "not_in_audience";

export type RunRowPerson = {
  slug: string;
  displayName: string;
  /** The person's own outcome: ok / skipped / error / cold_start / pending. */
  status: string;
  /** Their failure, when they have one — shown in the picks panel instead of an empty list. */
  error: string | null;
  decision: RowDecision | null;
  hasTrace: boolean;
  /** What this row delivered to them, per library. Empty when the row built nothing for them. */
  breakdown: RunLibraryBreakdown[];
  /** Present only for people still on the Users page — a departed account has no page to link to. */
  userId?: number;
};

/** One row as this run built it. Per-person rows carry their people; a shared row carries its result.
 *
 *  Both are rows, and that is the point of grouping this way: a run's unit of work is a ROW, and
 *  presenting it as a list of people left a shared row — which belongs to no person — nowhere to go. */
export type RunRowGroup = {
  slug: string;
  title: string;
  /** The libraries this row actually delivered to, e.g. ["Movies", "TV Shows"]. */
  libraries: string[];
  kind: "per_person" | "shared";
  people: RunRowPerson[];
  shared: RunSharedRowResult | null;
};

export type RunRowsView = {
  /** Rows this run BUILT (or meant to). */
  groups: RunRowGroup[];
  /** Rows that exist but were not part of this run — a scoped run's unselected rows. */
  notInRun: { slug: string; title: string }[];
};

const DECISIONS = new Set<string>([
  "due",
  "not_due",
  "muted",
  "not_in_audience",
]);

function asDecision(value: unknown): RowDecision | null {
  return typeof value === "string" && DECISIONS.has(value)
    ? (value as RowDecision)
    : null;
}

/**
 * A row's own name, with its `{placeholder}` segments removed.
 *
 * A row is configured as a TEMPLATE — `✨ {library_name} Picked for You` — and renders once per
 * library, so it has as many delivered titles as it has libraries. Using one of them as the row's
 * name picks a winner arbitrarily and hides the rest: a run that built both Movies and TV Shows
 * displayed only "✨ TV Shows Picked for You", which reads as Movies never having run. The libraries
 * belong beside the name as their own field, not inside it.
 *
 * Rendering the raw template instead is no good either — a row that delivered nothing has no
 * rendered title anywhere, and `{library_name}` leaked onto the page. Stripping gives one stable
 * name for the row in every state.
 */
export function rowDisplayName(name: string): string {
  return name
    .replace(/\{[^}]*\}/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/**
 * Group a run by ROW rather than by person, scoped to the rows the run actually ran.
 *
 * @param run The run detail payload.
 * @param titles Row slug → the row's configured name (template form). The source of a row's
 *   identity; delivered titles are only a fallback for a row no longer in the config.
 * @param idBySlug User slug → id, for deep-linking a person. Someone removed from Plex since the
 *   run will not be in it.
 */
export function groupRunByRow(
  run: RunDetail,
  titles: Record<string, string> = {},
  idBySlug: Map<string, number> = new Map(),
): RunRowsView {
  // A row is IN this run if the run meant to build it for somebody, or actually delivered it.
  // Everything else is a row that merely exists — listing those made a scoped run ("rebuild just
  // this row") look like it had touched rows the operator never selected.
  const ran = new Set<string>();
  const considered = new Set<string>();
  const delivered = new Map<string, string>();
  for (const user of run.users) {
    for (const [slug, decision] of Object.entries(user.rows_considered ?? {})) {
      considered.add(slug);
      if (decision === "due") ran.add(slug);
    }
    for (const entry of user.breakdown ?? []) {
      if (!entry.row_slug) continue;
      ran.add(entry.row_slug);
      if (entry.row_title) delivered.set(entry.row_slug, entry.row_title);
    }
  }

  const nameFor = (slug: string): string =>
    rowDisplayName(titles[slug] ?? delivered.get(slug) ?? slug) || slug;

  const groups = new Map<string, RunRowGroup>();
  const ensure = (slug: string, kind: RunRowGroup["kind"]): RunRowGroup => {
    let group = groups.get(slug);
    if (!group) {
      group = {
        slug,
        title: nameFor(slug),
        libraries: [],
        kind,
        people: [],
        shared: null,
      };
      groups.set(slug, group);
    }
    return group;
  };

  for (const user of run.users) {
    const decisions = user.rows_considered ?? {};
    const breakdown = user.breakdown ?? [];
    const slugs = new Set<string>(Object.keys(decisions));
    for (const entry of breakdown)
      if (entry.row_slug) slugs.add(entry.row_slug);
    for (const slug of slugs) {
      if (!ran.has(slug)) continue;
      const mine = breakdown.filter((entry) => entry.row_slug === slug);
      ensure(slug, "per_person").people.push({
        slug: user.slug,
        displayName: user.display_name,
        status: user.status,
        error: user.error,
        decision: asDecision(decisions[slug]),
        hasTrace: user.has_trace,
        breakdown: mine,
        userId: idBySlug.get(user.slug),
      });
    }
  }

  // Libraries, in the order the run delivered them, deduped.
  for (const group of groups.values()) {
    const seen = new Set<string>();
    for (const person of group.people) {
      for (const entry of person.breakdown) {
        if (entry.library_title && !seen.has(entry.library_title)) {
          seen.add(entry.library_title);
          group.libraries.push(entry.library_title);
        }
      }
    }
  }

  const shared: RunRowGroup[] = (run.shared_rows ?? []).map((row) => {
    const libraries: string[] = [];
    for (const entry of row.breakdown ?? []) {
      if (entry.library_title && !libraries.includes(entry.library_title)) {
        libraries.push(entry.library_title);
      }
    }
    return {
      slug: row.collection_slug,
      title:
        rowDisplayName(titles[row.collection_slug] ?? row.row_title ?? "") ||
        row.collection_slug,
      libraries,
      kind: "shared" as const,
      people: [],
      shared: row,
    };
  });

  const notInRun = [...considered]
    .filter((slug) => !ran.has(slug))
    .map((slug) => ({ slug, title: nameFor(slug) }))
    .sort((a, b) => a.title.localeCompare(b.title));

  const byTitle = (a: RunRowGroup, b: RunRowGroup) =>
    a.title.localeCompare(b.title);
  return {
    // Per-person first, then shared: a shared row is the odd one out on most servers, and burying it
    // between two per-person rows is how it went unnoticed to begin with.
    groups: [...[...groups.values()].sort(byTitle), ...shared.sort(byTitle)],
    notInRun,
  };
}

/** Counts behind a per-person row's one-line summary. */
export function rowCounts(group: RunRowGroup): {
  people: number;
  built: number;
  failed: number;
  muted: number;
  notInAudience: number;
  unrecorded: number;
} {
  const counts = {
    people: group.people.length,
    built: 0,
    failed: 0,
    muted: 0,
    notInAudience: 0,
    unrecorded: 0,
  };
  for (const person of group.people) {
    if (person.status === "error") counts.failed += 1;
    else if (person.decision === "muted") counts.muted += 1;
    else if (person.decision === "not_in_audience") counts.notInAudience += 1;
    else if (person.breakdown.length > 0 || person.status === "ok")
      counts.built += 1;
    else if (person.decision === null) counts.unrecorded += 1;
  }
  return counts;
}

/** The line under a row's name: "46 of 46 built", or a shared row's own result. */
export function rowSummary(group: RunRowGroup): string {
  if (group.kind === "shared") {
    const row = group.shared;
    if (!row) return "";
    const picks = row.picks.length;
    const added = row.diff?.added?.length ?? 0;
    const removed = row.diff?.removed?.length ?? 0;
    const parts = [`${picks} ${picks === 1 ? "pick" : "picks"}`];
    if (added || removed) parts.push(`+${added} −${removed}`);
    return parts.join(" · ");
  }
  const c = rowCounts(group);
  const parts = [`${c.built} of ${c.people} built`];
  if (c.failed) parts.push(`${c.failed} failed`);
  if (c.muted) parts.push(`${c.muted} muted`);
  if (c.notInAudience) parts.push(`${c.notInAudience} not in the audience`);
  // Said out loud rather than folded in: a run from before the app recorded this cannot say why,
  // and guessing would put a confident wrong answer on every historical run.
  if (c.unrecorded) parts.push(`${c.unrecorded} not recorded`);
  return parts.join(" · ");
}

/** "Movies · TV Shows", or "" when the row delivered nothing to name. */
export function libraryLabel(group: RunRowGroup): string {
  return group.libraries.join(" · ");
}
