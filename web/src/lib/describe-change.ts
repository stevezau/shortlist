/**
 * One audit event, said in a sentence: the Activity page's "Changes on Plex" tab.
 *
 * Every write Shortlist makes to Plex or plex.tv leaves an `events` row whose `message` is the
 * writer's own structured diff (plex-safety rule 10). The keys differ per scope, so this file is the
 * one place that knows each shape — read from the emitter, cited beside each case. A scope it does not
 * know still renders: its name, and a one-line summary of the message.
 *
 * Filter rules inside a sentence are wrapped in backticks (`label!=shortlist_kid`) so the table can set
 * them in the code face; everything else is plain text.
 */
import type { AuditEvent } from "@/lib/types";

export interface ChangeDescription {
  /** The person, row or account the change was made to. */
  who: string;
  /** Which part of it: a collection title, a library, "Share filter". */
  what: string;
  /** What happened, or for a dry run what would have. */
  change: string;
}

/** Display names the page already has, so a sentence can say "Movie night" rather than its slug. */
export interface NameLookup {
  row?: (slug: string) => string | undefined;
  person?: (slug: string) => string | undefined;
}

/**
 * real / dry run come from the message's own `dry_run`. "setting" is the one `collection.poster` shape
 * that changed only the stored setting. "unrecorded" is a scope that writes no `dry_run` at all
 * (renames, the restriction repair) — saying "Real" there would be a guess.
 */
export type ChangeMode = "real" | "dry" | "setting" | "unrecorded";

type Message = Record<string, unknown>;

const MAX_SUMMARY = 160;

/** What a `run.demote` entry's `reason` code means (pipeline.py converge). */
const DEMOTE_REASONS: Record<string, string> = {
  paused: "its person is paused",
  shared_row_switched_off: "the shared row is switched off",
  unknown_owner: "nobody Shortlist knows owns it",
  on_owner_home: "it was on your own Home",
};

/** The row-edit scopes that remove collections (row_changes.py, collections.py), by cause. */
const REMOVAL_CAUSES: Record<string, string> = {
  "collection.build": "Changed how it's built",
  "collection.audience": "People taken out of its audience",
  "collection.disable": "Switched off",
  "collection.libraries": "Left some libraries",
  "collection.delete": "Row deleted",
  "collection.cleanup": "Removed from Plex now",
};

const RENAME_CAUSES: Record<string, string> = {
  "collection.rename": "Renamed",
  "settings.rename": "Name changed in Settings",
  "user.nickname": "Nickname changed",
};

/** `PosterOut.mode`, as the poster the owner chose. */
const POSTER_MODES: Record<string, string> = {
  "": "Plex's own artwork",
  ai: "an AI-made poster",
  generate: "a generated poster",
  text: "a text poster",
  upload: "an uploaded image",
};

const SHARED_PREFIX = "shared_";
const LABEL_PREFIX = "shortlist_";
const HELPER_PREFIX = "freed-name helper:";

// --- reading the open map --------------------------------------------------------------------------

function str(value: unknown): string {
  return typeof value === "string" ? value : "";
}

function num(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function records(value: unknown): Message[] {
  return Array.isArray(value) ? value.filter((v): v is Message => typeof v === "object" && v !== null) : [];
}

function record(value: unknown): Message {
  return typeof value === "object" && value !== null && !Array.isArray(value) ? (value as Message) : {};
}

// --- wording ---------------------------------------------------------------------------------------

function plural(count: number, one: string, many = `${one}s`): string {
  return `${count} ${count === 1 ? one : many}`;
}

/** "a", "a and b", "a, b and c". */
function joinAnd(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** Up to three names, then "and N more" — a sweep can delete forty rows. */
function nameList(items: string[], max = 3): string {
  if (items.length <= max) return joinAnd(items);
  return `${items.slice(0, max).join(", ")} and ${items.length - max} more`;
}

function unique(items: string[]): string[] {
  return [...new Set(items.filter(Boolean))];
}

function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

// --- one helper per message shape -----------------------------------------------------------------

/** `CollectionDiff.__dict__` (engine/models.py) — what delivery changed on one row. "" for none. */
function diffSentence(diff: Message, dry: boolean): string {
  const added = strings(diff.added).length;
  const removed = strings(diff.removed).length;
  const deleted = strings(diff.deleted);
  const duplicates = strings(diff.duplicates_removed).length;
  const created = diff.created === true;
  if (dry) {
    const parts = [
      added ? `add ${plural(added, "title")}` : "",
      removed ? `remove ${plural(removed, "title")}` : "",
      created ? "create the collection" : "",
      deleted.length ? `delete ${nameList(deleted)}` : "",
      duplicates ? `remove ${plural(duplicates, "leftover copy", "leftover copies")}` : "",
    ].filter(Boolean);
    return parts.length ? `Would ${joinAnd(parts)}` : "";
  }
  const parts = [
    added ? `+${plural(added, "title")}` : "",
    removed ? `−${plural(removed, "title")}` : "",
    created ? "collection created" : "",
    deleted.length ? `deleted ${nameList(deleted)}` : "",
    duplicates ? `${plural(duplicates, "leftover copy", "leftover copies")} removed` : "",
  ].filter(Boolean);
  return capitalise(parts.join(", "));
}

function rowFailed(message: Message): boolean {
  return message.status === "error" || str(message.error) !== "";
}

/**
 * One share-filter string as a set of single rules, `label!=shortlist_kid` each.
 *
 * Conditions are joined by `&` (Plex Web) or `|` (plexapi), and a condition's values by `,` or plexapi's
 * `%2C` (tests/fixtures/plextv_combined_filters.json) — so splitting on all four is what lets a
 * before/after pair name only the rule that actually changed, whichever writer last touched the account.
 */
function filterRules(filter: string): Set<string> {
  const rules = new Set<string>();
  for (const condition of filter.split(/[&|]/)) {
    const match = /^\s*([A-Za-z.]+)\s*(!=|=)(.*)$/.exec(condition);
    if (!match) continue;
    const [, field, op, values = ""] = match;
    for (const raw of values.split(/,|%2C/i)) {
      let value = raw.trim();
      try {
        value = decodeURIComponent(value.replace(/\+/g, " "));
      } catch {
        // A stray `%` is the owner's literal text; show it as written.
      }
      if (value) rules.add(`${field}${op}${value}`);
    }
  }
  return rules;
}

function filterDelta(fields: Message): { added: string[]; removed: string[] } {
  const added: string[] = [];
  const removed: string[] = [];
  for (const pair of Object.values(fields)) {
    const { before, after } = record(pair);
    const was = filterRules(str(before));
    const now = filterRules(str(after));
    for (const rule of now) if (!was.has(rule)) added.push(rule);
    for (const rule of was) if (!now.has(rule)) removed.push(rule);
  }
  return { added: unique(added), removed: unique(removed) };
}

const code = (rules: string[]) => joinAnd(rules.map((rule) => `\`${rule}\``));

function shareFilterSentence(message: Message, dry: boolean): string {
  const { added, removed } = filterDelta(record(message.fields));
  if (dry) {
    if (added.length && removed.length) return `Would add ${code(added)} and remove ${code(removed)}`;
    if (added.length) return `Would merge ${code(added)} into the share filter`;
    if (removed.length) return `Would remove ${code(removed)} from the share filter`;
    return "Would rewrite the share filter with the same rules";
  }
  const parts = [added.length ? `${code(added)} added` : "", removed.length ? `${code(removed)} removed` : ""].filter(
    Boolean,
  );
  return parts.length ? `Share filter merged: ${parts.join(", ")}` : "Share filter rewritten with the same rules";
}

/** `run.hub_order` / `shelf.order` (run_persistence.py:1574, jobs.py:1068). */
function shelfOrder(message: Message, dry: boolean): ChangeDescription {
  const moved = strings(message.moved).length;
  const rows = plural(moved, "row");
  let change: string;
  if (dry) change = `Would place ${rows} on the shelf`;
  else if (message.verified === false) change = `Asked Plex to place ${rows}, but the shelf didn't read back in that order`;
  else {
    // `repositioned` counts every hub the pass moved, ours and other tools'; more than ours means other
    // tools' rows were shifted to seat ours — keeping their order relative to each other (rule 4).
    const othersMoved = num(message.repositioned) > moved;
    change = `Shelf order set: ${rows} placed${othersMoved ? ", other tools' rows kept in their order" : ""}`;
  }
  return { who: "Recommended shelf", what: str(message.library), change };
}

function shelfUnplaced(message: Message): ChangeDescription {
  const anchor = str(message.anchor);
  const reason = str(message.reason) || "Plex refused the position";
  return {
    who: str(message.row) || "Recommended shelf",
    what: str(message.library),
    change: `Not placed on the shelf: ${reason}${anchor ? ` (${anchor})` : ""}`,
  };
}

function requestsSentence(message: Message, dry: boolean): string {
  const outcomes = records(message.outcomes);
  const titlesWith = (status: string) => outcomes.filter((o) => o.status === status).map((o) => str(o.title));
  const queued = num(message.queued);
  const failed = titlesWith("error").length;
  const asked = titlesWith(dry ? "would_request" : "requested");
  const askedCount = asked.length || num(message.sent);
  const parts = [
    askedCount ? `${dry ? "Would request" : "Requested"} ${asked.length ? nameList(asked) : plural(askedCount, "title")}` : "",
    queued ? `${plural(queued, "title")} waiting for your approval` : "",
    failed ? `${plural(failed, "request")} failed` : "",
  ].filter(Boolean);
  return parts.length ? capitalise(parts.join("; ")) : "Nothing requested";
}

function removalSentence(message: Message, dry: boolean): string {
  const removed = strings(message.removed);
  const error = str(message.error);
  if (error) return `Couldn't finish: ${error}${removed.length ? `. Removed first: ${nameList(removed)}` : ""}`;
  if (!removed.length) return "Nothing on Plex to remove";
  return `${dry ? "Would remove from Plex" : "Removed from Plex"}: ${nameList(removed)}`;
}

function renameSentence(message: Message): string {
  const renames = records(message.renames);
  const done = renames.filter((r) => r.next_run !== true);
  const later = renames.length - done.length;
  const error = str(message.error);
  const first = done[0];
  const arrow = first ? `“${str(first.old)}” → “${str(first.new)}”` : "";
  const parts: string[] = [];
  if (done.length === 1 && first) {
    const libraries = strings(first.libraries);
    parts.push(`${arrow}${libraries.length ? ` in ${joinAnd(libraries)}` : ""}`);
  } else if (done.length > 1) {
    parts.push(`Renamed ${done.length} collections, e.g. ${arrow}`);
  }
  if (later) parts.push(`${later} ${later === 1 ? "is" : "are"} retitled on ${later === 1 ? "its" : "their"} next run`);
  if (error) parts.push(`couldn't rename everything: ${error}`);
  return parts.length ? capitalise(parts.join("; ")) : "Nothing on Plex needed renaming";
}

function uninstallSummary(message: Message, dry: boolean): string {
  const restored = num(message.filters_restored);
  const deleted = strings(message.collections_deleted).length;
  const disabled = num(message.rows_disabled);
  const failed = records(message.filters_failed).length;
  const unreachable = records(message.filters_unreachable).length;
  const done = dry
    ? `Would ${joinAnd([
        `restore ${plural(restored, "share filter")}`,
        `delete ${plural(deleted, "collection")}`,
        `switch off ${plural(disabled, "row")}`,
      ])}`
    : `${joinAnd([
        `${plural(restored, "share filter")} restored`,
        `${plural(deleted, "collection")} deleted`,
        `${plural(disabled, "row")} switched off`,
      ])}`;
  const problems = [
    failed ? `${plural(failed, "share filter")} couldn't be restored` : "",
    unreachable ? `${plural(unreachable, "account")} couldn't be reached` : "",
    str(message.stopped_by) ? `stopped partway: ${str(message.stopped_by)}` : "",
  ].filter(Boolean);
  return [done, ...problems].join("; ");
}

/** The fallback for a scope this file does not know: `key: value` pairs, one line. */
function summarise(message: Message): string {
  const parts = Object.entries(message)
    .filter(([key]) => key !== "run_id" && key !== "dry_run")
    .map(([key, value]) => {
      if (Array.isArray(value)) return `${key}: ${plural(value.length, "item")}`;
      if (typeof value === "object" && value !== null) return `${key}: {…}`;
      return `${key}: ${String(value)}`;
    });
  const text = parts.join(", ");
  return text.length > MAX_SUMMARY ? `${text.slice(0, MAX_SUMMARY - 1)}…` : text;
}

// --- the public functions --------------------------------------------------------------------------

/**
 * Who a change was made to, which part of them, and what changed — for one audit event.
 *
 * Args:
 *   event: One row of `GET /api/events/log`.
 *   names: Optional lookups for display names; without them rows and people show as their slugs.
 *
 * Returns:
 *   The three strings the table shows. `change` wraps filter rules in backticks.
 */
export function describeChange(event: AuditEvent, names: NameLookup = {}): ChangeDescription {
  const m = event.message;
  const dry = m.dry_run === true;
  const rowName = (slug: string) => names.row?.(slug) ?? slug;
  const personName = (slug: string) => names.person?.(slug) ?? slug;

  switch (event.scope) {
    case "run.user": {
      // run_persistence.py:1419 — one per person per run.
      const diff = record(m.diff);
      const change = rowFailed(m)
        ? `Failed: ${str(m.error) || "the run reported an error"}`
        : diffSentence(diff, dry) || "Nothing changed on Plex";
      return { who: personName(str(m.user)), what: str(diff.collection_title) || "Their row", change };
    }
    case "run.shared": {
      // run_persistence.py:1187 — `row` is the report slug, `shared_<row slug>`.
      const diff = record(m.diff);
      const slug = str(m.row).startsWith(SHARED_PREFIX) ? str(m.row).slice(SHARED_PREFIX.length) : str(m.row);
      const reason = str(m.reason);
      const change = rowFailed(m)
        ? `Failed: ${str(m.error) || "the run reported an error"}`
        : diffSentence(diff, dry) || (reason ? `Built nothing: ${reason}` : "Nothing changed on Plex");
      return { who: str(diff.collection_title) || rowName(slug), what: "Shared row", change };
    }
    case "run.sweep": {
      // run_persistence.py:1454 — `deleted` is {user slug: [titles]}; helper keys are not rows.
      const deleted = record(m.deleted);
      const people = Object.keys(deleted).filter((key) => !key.startsWith(HELPER_PREFIX));
      const helpers = Object.keys(deleted)
        .filter((key) => key.startsWith(HELPER_PREFIX))
        .flatMap((key) => strings(deleted[key])).length;
      const titles = people.flatMap((key) => strings(deleted[key]));
      const rows = titles.length
        ? `${plural(titles.length, "row")} no share filter could hide: ${nameList(titles)}`
        : "";
      const helperText = helpers ? `${plural(helpers, "leftover helper collection")}` : "";
      const what = [rows, helperText].filter(Boolean).join(", and ");
      return {
        who: people.length ? nameList(people.map(personName)) : "Leftover helpers",
        what: "Swept before delivery",
        change: `${dry ? "Would delete" : "Deleted"} ${what}`,
      };
    }
    case "run.orphan_delete": {
      // run_persistence.py:1512 — collections whose label names nobody Shortlist knows.
      const deleted = records(m.deleted);
      return {
        who: "Rows nobody owns",
        what: joinAnd(unique(deleted.map((d) => str(d.library)))),
        change: `${dry ? "Would delete" : "Deleted"} ${plural(deleted.length, "collection")} whose person is no longer on the server: ${nameList(deleted.map((d) => str(d.title)))}`,
      };
    }
    case "run.demote": {
      // run_persistence.py:1487 — each entry names its row by label, `shortlist_<slug>`.
      const demoted = records(m.demoted);
      const people = unique(
        demoted.map((d) => str(d.label)).map((label) => (label.startsWith(LABEL_PREFIX) ? label.slice(LABEL_PREFIX.length) : label)),
      );
      const reasons = unique(demoted.map((d) => str(d.reason)));
      const why = reasons.length === 1 && reasons[0] ? DEMOTE_REASONS[reasons[0]] : undefined;
      return {
        who: nameList(people.map(personName)),
        what: joinAnd(unique(demoted.map((d) => str(d.library)))),
        change: `${dry ? "Would take off Home" : "Taken off Home"}: ${nameList(demoted.map((d) => str(d.title)))}${why ? ` (${why})` : ""}`,
      };
    }
    case "run.privacy_sync":
      // run_persistence.py:1544 — one per account whose filter was written; before/after per field.
      return { who: str(m.username), what: "Share filter", change: shareFilterSentence(m, dry) };
    case "run.hub_order":
    case "shelf.order":
      return shelfOrder(m, dry);
    case "run.hub_unplaced":
    case "shelf.unplaced":
      return shelfUnplaced(m);
    case "run.requests":
      // run_persistence.py:1644 — one per run that queued or sent anything.
      return { who: "Requests", what: `${plural(num(m.considered), "title")} considered`, change: requestsSentence(m, dry) };
    case "collection.poster": {
      const who = rowName(str(m.slug));
      if (!("poster_reset" in m)) {
        // collections.py:1411/2233 — only the stored setting changed.
        const poster = POSTER_MODES[str(m.mode)] ?? str(m.mode);
        return { who, what: "Poster setting", change: `Poster setting saved as ${poster}. This change wrote nothing to Plex` };
      }
      // collection_reconcile.py:710 — the custom poster dropped, so Plex's own artwork goes back on.
      const reset = strings(m.poster_reset);
      const error = str(m.error);
      const change = error
        ? `Couldn't reset the poster: ${error}`
        : reset.length
          ? `${dry ? "Would reset the poster" : "Poster reset"} to Plex's own artwork in ${joinAnd(reset)}`
          : "No collection on Plex needed its poster reset";
      return { who, what: "Poster", change };
    }
    case "collection.build":
    case "collection.audience":
    case "collection.disable":
    case "collection.libraries":
    case "collection.delete":
    case "collection.cleanup":
      // collection_reconcile.py:737 — the scope is the cause the caller passed in.
      return { who: rowName(str(m.slug)), what: REMOVAL_CAUSES[event.scope] ?? "", change: removalSentence(m, dry) };
    case "collection.rename":
    case "settings.rename":
    case "user.nickname": {
      // collection_reconcile.py:1167 — for a nickname the person is the subject, not the row.
      const first = records(m.renames)[0];
      const who =
        event.scope === "user.nickname" && first
          ? str(first.display_name) || personName(str(first.user))
          : rowName(str(m.slug));
      return { who, what: RENAME_CAUSES[event.scope] ?? "", change: renameSentence(m) };
    }
    case "user.disable.cleanup": {
      // jobs.py:1428 — a person switched off: their rows come down.
      const removed = strings(m.removed);
      const change = removed.length
        ? `${dry ? "Would remove from Plex" : "Rows removed from Plex"}: ${nameList(removed)}`
        : "No rows on Plex to remove";
      return { who: personName(str(m.user)), what: "Turned off", change };
    }
    case "user.pause.hide": {
      // jobs.py:1461 — pausing demotes every surface; the collections and labels stay.
      const hidden = num(m.hidden);
      const change = !hidden
        ? "Nothing was showing, so nothing came down"
        : dry
          ? `Would take ${plural(hidden, "row")} off Home and Recommended`
          : `${plural(hidden, "row")} taken off Home and Recommended; the collections stay on Plex`;
      return { who: personName(str(m.user)), what: "Paused", change };
    }
    case "uninstall.user": {
      // api/system.py:958 — `user` is the Plex username. Two shapes: restored, or written but unverified.
      const change =
        m.verified === false
          ? `Restore not confirmed: ${str(m.error) || "plex.tv did not read back the old filter"}`
          : dry
            ? "Would put the share filter back the way it was before Shortlist"
            : "Share filter put back the way it was before Shortlist";
      return { who: str(m.user), what: "Uninstall", change };
    }
    case "system.uninstall":
      // api/system.py:963 — the whole-server summary of an uninstall.
      return { who: "Whole server", what: "Uninstall", change: uninstallSummary(m, dry) };
    case "privacy.restriction_restored":
      // audit.py:118 (#116) — a `|` the pre-#116 merge wrote had switched the owner's own restriction off.
      return { who: str(m.username), what: "Share filter", change: "Repaired: the restriction you set in Plex applies again" };
    default:
      return { who: event.scope, what: "", change: summarise(m) };
  }
}

/** Whether the change was real, a dry run, a setting only, or does not say. */
export function changeMode(event: AuditEvent): ChangeMode {
  const m = event.message;
  if (m.dry_run === true) return "dry";
  if (m.dry_run === false) return "real";
  if (event.scope === "collection.poster" && !("poster_reset" in m)) return "setting";
  return "unrecorded";
}

/** Whether the change failed, or did not take: an error level or message, or a write Plex did not keep. */
export function changeFailed(event: AuditEvent): boolean {
  const m = event.message;
  return (
    event.level === "error" ||
    str(m.error) !== "" ||
    m.verified === false ||
    records(m.filters_failed).length > 0
  );
}

/** The run this change was part of; null for a job's pass or an edit made in the app. */
export function changeRunId(event: AuditEvent): number | null {
  const runId = event.message.run_id;
  return typeof runId === "number" ? runId : null;
}

/**
 * A person's or shared row's run record that wrote nothing to Plex. Every person gets a `run.user`
 * event every run, so on a server of forty people most nights are mostly these; the table counts them
 * rather than listing them. A failure is never one of these.
 */
export function isNoOpChange(event: AuditEvent): boolean {
  if (event.scope !== "run.user" && event.scope !== "run.shared") return false;
  if (changeFailed(event)) return false;
  return diffSentence(record(event.message.diff), false) === "";
}

/** The jobs that run a privacy or shelf pass without a run, in the Jobs tab's words. */
const JOB_LABELS: Record<string, string> = {
  "privacy.sync": "Privacy sync",
  "sync.check": "Check and fix rows on Plex",
  "sync.users": "Sync people from Plex",
};

export function jobLabel(kind: string): string {
  return JOB_LABELS[kind] ?? kind;
}
