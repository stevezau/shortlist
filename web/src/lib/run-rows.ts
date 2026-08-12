import type { RunDetail, RunSharedRowResult } from "@/lib/types";

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
  decision: RowDecision | null;
  hasTrace: boolean;
  /** Present only for people still on the Users page — a departed account has no page to link to. */
  userId?: number;
};

/** One row as a run touched it. Per-person rows carry their people; a shared row carries its result.
 *
 *  Both are rows, and that is the whole point of grouping this way: a run's unit of work is a ROW,
 *  and presenting it as a list of people left a shared row — which belongs to no person — with
 *  nowhere to appear at all. */
export type RunRowGroup = {
  slug: string;
  title: string;
  kind: "per_person" | "shared";
  people: RunRowPerson[];
  shared: RunSharedRowResult | null;
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
 * Group a run by ROW rather than by person.
 *
 * Per-person rows are assembled from two sources, because neither alone is complete: `breakdown`
 * only exists for someone who actually built something, and `rows_considered` only exists on runs
 * recorded since it was added. A skipped person has an empty breakdown — and on a run where nothing
 * was due, that is everybody — so without `rows_considered` the largest group on the page would fall
 * outside the tree entirely.
 *
 * @param run The run detail payload.
 * @param titles Row slug → display name, for rows nothing delivered a title for this run. A run
 *   where every person was skipped has no `row_title` anywhere, so the slug is all that survives
 *   without this. Falls back to the slug, never to a blank.
 * @param idBySlug User slug → id, for deep-linking a person to their page. Someone removed from
 *   Plex since the run will not be in it.
 */
export function groupRunByRow(
  run: RunDetail,
  titles: Record<string, string> = {},
  idBySlug: Map<string, number> = new Map(),
): RunRowGroup[] {
  const perPerson = new Map<string, RunRowGroup>();
  // Titles AS DELIVERED win over the current name: a row renamed since the run must not rewrite what
  // that run says it built.
  const delivered = new Map<string, string>();
  for (const user of run.users) {
    for (const entry of user.breakdown ?? []) {
      if (entry.row_slug && entry.row_title)
        delivered.set(entry.row_slug, entry.row_title);
    }
  }

  const ensure = (slug: string): RunRowGroup => {
    let group = perPerson.get(slug);
    if (!group) {
      group = {
        slug,
        title: delivered.get(slug) ?? titles[slug] ?? slug,
        kind: "per_person",
        people: [],
        shared: null,
      };
      perPerson.set(slug, group);
    }
    return group;
  };

  for (const user of run.users) {
    const considered = user.rows_considered ?? {};
    const slugs = new Set<string>(Object.keys(considered));
    for (const entry of user.breakdown ?? []) {
      if (entry.row_slug) slugs.add(entry.row_slug);
    }
    for (const slug of slugs) {
      ensure(slug).people.push({
        slug: user.slug,
        displayName: user.display_name,
        status: user.status,
        decision: asDecision(considered[slug]),
        hasTrace: user.has_trace,
        userId: idBySlug.get(user.slug),
      });
    }
  }

  const shared: RunRowGroup[] = (run.shared_rows ?? []).map((row) => ({
    slug: row.collection_slug,
    title: row.row_title || titles[row.collection_slug] || row.collection_slug,
    kind: "shared" as const,
    people: [],
    shared: row,
  }));

  // Per-person rows first, then shared: a shared row is the odd one out on most servers, and burying
  // it between two per-person rows is how it went unnoticed in the first place.
  const sortByTitle = (a: RunRowGroup, b: RunRowGroup) =>
    a.title.localeCompare(b.title);
  return [
    ...[...perPerson.values()].sort(sortByTitle),
    ...shared.sort(sortByTitle),
  ];
}

/** Counts behind a per-person row's one-line summary. */
export function rowCounts(group: RunRowGroup): {
  people: number;
  built: number;
  notDue: number;
  muted: number;
  notInAudience: number;
  failed: number;
  unrecorded: number;
} {
  const counts = {
    people: group.people.length,
    built: 0,
    notDue: 0,
    muted: 0,
    notInAudience: 0,
    failed: 0,
    unrecorded: 0,
  };
  for (const person of group.people) {
    if (person.status === "error") counts.failed += 1;
    if (person.decision === null) counts.unrecorded += 1;
    else if (person.decision === "not_due") counts.notDue += 1;
    else if (person.decision === "muted") counts.muted += 1;
    else if (person.decision === "not_in_audience") counts.notInAudience += 1;
    // "due" is intent — it only counts as BUILT if the person's own run actually succeeded.
    else if (person.status === "ok") counts.built += 1;
  }
  return counts;
}

/** "46 people · 0 built, 46 not due" — the line under a per-person row's name.
 *
 *  Only reasons that actually apply are listed, so the common case reads as one short clause rather
 *  than four zeroes. */
export function rowSummary(group: RunRowGroup): string {
  if (group.kind === "shared") {
    const row = group.shared;
    if (!row) return "";
    const added = row.diff?.added?.length ?? 0;
    const removed = row.diff?.removed?.length ?? 0;
    const picks = row.picks.length;
    const parts = [`${picks} ${picks === 1 ? "pick" : "picks"}`];
    if (added || removed) parts.push(`+${added} −${removed}`);
    return parts.join(" · ");
  }
  const c = rowCounts(group);
  const people = `${c.people} ${c.people === 1 ? "person" : "people"}`;
  const reasons: string[] = [`${c.built} built`];
  if (c.failed) reasons.push(`${c.failed} failed`);
  if (c.notDue) reasons.push(`${c.notDue} not due`);
  if (c.muted) reasons.push(`${c.muted} muted`);
  if (c.notInAudience) reasons.push(`${c.notInAudience} not in the audience`);
  // Said out loud rather than folded into "not due": a run from before the app recorded this cannot
  // say why, and guessing would put a confident wrong answer on every historical run.
  if (c.unrecorded) reasons.push(`${c.unrecorded} not recorded`);
  return `${people} · ${reasons.join(", ")}`;
}
