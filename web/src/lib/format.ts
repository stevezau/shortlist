import { fillPlaceholders, LIBRARY_NAME } from "@/lib/placeholders";

/** "6h ago" style relative time for ISO timestamps; plain English on edge cases. `now` lets a caller
 *  that already ticks a clock (a live run's row) share it, so its labels can't disagree. */
export function timeAgo(iso: string | null, now: number = Date.now()): string {
  if (!iso) return "never";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "unknown";
  const seconds = Math.max(0, Math.floor((now - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

/** "in 6h" style relative time for a FUTURE ISO timestamp. */
export function timeUntil(iso: string | null): string {
  if (!iso) return "—";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "—";
  const seconds = Math.max(0, Math.floor((then - Date.now()) / 1000));
  if (seconds < 60) return "in <1m";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `in ${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const remMin = minutes % 60;
  if (hours < 24)
    return remMin > 0 ? `in ${hours}h ${remMin}m` : `in ${hours}h`;
  const days = Math.floor(hours / 24);
  return `in ${days}d`;
}

export function formatDate(
  iso: string | null,
  opts: { dateOnly?: boolean } = {},
): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  // `dateOnly` for a date that answers "roughly when", where a time of day is noise pretending to be
  // precision — "around 23 Aug 2026" reads as an estimate; "23 Aug 2026, 16:30" reads as a deadline.
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    ...(opts.dateOnly ? {} : { hour: "2-digit", minute: "2-digit" }),
  });
}

/**
 * The Monday a `%Y-%W` week bucket starts on, as "6 Jul" — the trend chart's x-axis.
 *
 * `%W` (SQLite's, via `report_service`) counts from the year's FIRST MONDAY: week 01 begins there,
 * and the days before it are week 00. The buckets are cut in UTC while this renders in the reader's
 * own calendar, so at a timezone boundary a label can sit a day off the bucket's true edge — it is
 * a "week commencing" caption, not a timestamp. An unparseable bucket is returned verbatim rather
 * than guessed at.
 */
export function weekStarting(week: string): string {
  const match = /^(\d{4})-(\d{1,2})$/.exec(week);
  if (!match) return week;
  const year = Number(match[1]);
  const index = Number(match[2]);
  // Sunday is 0 in JS, so `(8 - day) % 7` is the distance to the first Monday from any start day.
  const toFirstMonday = (8 - new Date(year, 0, 1).getDay()) % 7;
  // Built from date COMPONENTS, not by adding milliseconds: a week that spans a DST change is 23 or
  // 25 hours long, and arithmetic on the epoch drifts the label onto the wrong day twice a year.
  const dayOfYear = index === 0 ? 1 : 1 + toFirstMonday + (index - 1) * 7;
  return new Date(year, 0, dayOfYear).toLocaleDateString(undefined, {
    day: "numeric",
    month: "short",
  });
}

/** Bytes → "512 B" / "48 KB" / "1.2 MB", for a backup file listing. */
export function formatSize(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

/** A duration in ms, or "—" when there isn't one yet — a run user who hasn't started has
 *  `duration_ms: null`, which must not render as the literal "nullms". */
export function formatDuration(ms: number | null): string {
  if (ms === null) return "—";
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes >= 60) {
    const total = Math.round(seconds / 60);
    return `${Math.floor(total / 60)}h ${total % 60}m`;
  }
  return `${minutes}m ${Math.round(seconds % 60)}s`;
}

/** Wall-clock a run took: finished − started in ms, or null while it's still running / unparseable. */
export function runElapsedMs(
  startedAt: string | null,
  finishedAt: string | null,
): number | null {
  if (!startedAt || !finishedAt) return null;
  const start = Date.parse(startedAt);
  const end = Date.parse(finishedAt);
  if (Number.isNaN(start) || Number.isNaN(end) || end < start) return null;
  return end - start;
}

/** Map a run/user status string onto a badge tone. */
export function runStatusVariant(
  status: string,
): "success" | "destructive" | "secondary" {
  if (status === "ok" || status === "success" || status === "finished")
    return "success";
  if (status === "error" || status === "failed") return "destructive";
  return "secondary";
}

const RUN_STATUS_LABELS: Record<string, string> = {
  ok: "OK",
  success: "OK",
  finished: "OK",
  error: "Failed",
  failed: "Failed",
  cold_start: "Cold start",
  skipped: "Skipped",
  pending: "Pending",
  running: "Running",
};

/** Keyed on the server's own trigger words (`Run.trigger`). */
const TRIGGER_LABELS: Record<string, string> = {
  schedule: "Scheduled",
  manual: "Manual",
  assistant: "Assistant",
  wizard: "Setup",
  // A scheduled run a restart cut short, finished for the people it never reached.
  resume: "Resumed after a restart",
};

/** A run/user status as a person reads it — never the raw enum ("cold_start" → "Cold start"). */
export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status.replace(/_/g, " ");
}

/** A run trigger as a person reads it ("manual" → "Manual"). */
export function triggerLabel(trigger: string): string {
  return TRIGGER_LABELS[trigger] ?? trigger.replace(/_/g, " ");
}

/** Narrow an unknown settings value to a string, else fall back. */
export function settingString(
  settings: Record<string, unknown>,
  key: string,
  fallback = "",
): string {
  const value = settings[key];
  return typeof value === "string" ? value : fallback;
}

/** Narrow an unknown settings value to a number, else fall back. */
export function settingNumber(
  settings: Record<string, unknown>,
  key: string,
  fallback: number,
): number {
  const value = settings[key];
  return typeof value === "number" && Number.isFinite(value) ? value : fallback;
}

/** Narrow an unknown settings value to a boolean, else fall back. */
export function settingBool(
  settings: Record<string, unknown>,
  key: string,
  fallback = false,
): boolean {
  const value = settings[key];
  return typeof value === "boolean" ? value : fallback;
}

/** "1 request", "3 requests": a count with its noun, for nouns that pluralise with an s. */
export function plural(count: number, noun: string, irregularPlural = `${noun}s`): string {
  return `${count} ${count === 1 ? noun : irregularPlural}`;
}

/** "rows" → "Rows". */
export function capitalise(text: string): string {
  return text.charAt(0).toUpperCase() + text.slice(1);
}

/** "a", "a and b", "a, b and c". */
export function joinList(items: string[]): string {
  if (items.length <= 1) return items[0] ?? "";
  return `${items.slice(0, -1).join(", ")} and ${items[items.length - 1]}`;
}

/** Row-name templates render {top_seed} from each user's history and {library_name} per library nightly. */
export function renderRowName(
  template: string,
  topSeed = "Fargo",
  user = "Sarah",
  libraryName = "Movies",
  season: { name: string; emoji: string } = { name: "Christmas", emoji: "🎄" },
  theme?: { name: string; emoji: string },
): string {
  // Fill EVERY placeholder with a sample value so the "on Plex this looks like" preview shows what
  // {user}/{top_seed}/{library_name}/{season} actually become — leaving any literal made the field
  // look broken.
  const rendered = fillPlaceholders(template, { topSeed, user, libraryName, season, theme });
  // A {library_name} title collapses its gap when the sample is empty, matching the backend renderer.
  return template.includes(LIBRARY_NAME)
    ? rendered.replace(/\s+/g, " ").trim()
    : rendered;
}

/**
 * The sample library a row's previews fill {library_name} with. It has to match the row's media type,
 * or a TV-only row previews as "More Movies to watch" — a name it can never produce. `media` is
 * derived from the libraries the row targets, so a row narrowed to TV libraries is "show" too.
 */
export function sampleLibraryName(media: string): string {
  return media === "show" ? "TV Shows" : "Movies";
}

/**
 * The sidebar footer's build line.
 *
 * `Shortlist · dev · 2ee14f8` on a pre-release build, `Shortlist · 1.4.0` on a release.
 *
 * The version is deliberately DROPPED on a pre-release. `current_version` is the last released
 * version, so on `:dev` it names the release this build came after — five commits ago, in the case
 * that prompted this — and printing it claims a version the running code is not. On `:dev` the
 * commit is the only thing that identifies a build anyway, since every push between two releases
 * reports the same version number. A release build is the mirror image: the tag IS the version, so
 * the sha would be noise. A source checkout has no provenance either way and shows its version.
 */
export function buildLabel(
  info?: {
    current_version?: string;
    git_branch?: string;
    git_sha?: string;
  } | null,
): string {
  // CI passes the branch on a dev push and the TAG (`v1.4.1`) on a release, which is what separates
  // the two cases — see `version_check.build_provenance`.
  const branch = info?.git_branch;
  const prerelease = Boolean(branch) && !branch!.startsWith("v");
  const parts = prerelease
    ? [branch, info?.git_sha?.slice(0, 7)]
    : [info?.current_version];
  return [
    "Shortlist",
    ...parts.filter((part): part is string => Boolean(part)),
  ].join(" · ");
}
