/** "6h ago" style relative time for ISO timestamps; plain English on edge cases. */
export function timeAgo(iso: string | null): string {
  if (!iso) return "never";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "unknown";
  const seconds = Math.max(0, Math.floor((Date.now() - then) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  return `${days}d ago`;
}

export function formatDate(iso: string | null): string {
  if (!iso) return "—";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

export function formatDuration(ms: number): string {
  if (ms < 1000) return `${ms}ms`;
  const seconds = ms / 1000;
  if (seconds < 60) return `${seconds.toFixed(1)}s`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes}m ${Math.round(seconds % 60)}s`;
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

const TRIGGER_LABELS: Record<string, string> = {
  manual: "Manual",
  scheduled: "Scheduled",
  cron: "Scheduled",
};

/** A run/user status as a person reads it — never the raw enum ("cold_start" → "Cold start"). */
export function runStatusLabel(status: string): string {
  return RUN_STATUS_LABELS[status] ?? status.replace(/_/g, " ");
}

/** A run trigger as a person reads it ("manual" → "Manual"). */
export function triggerLabel(trigger: string): string {
  return TRIGGER_LABELS[trigger] ?? trigger.replace(/_/g, " ");
}

/** hit_rate fraction (0..1) → "31%" or "—" before first measurement. */
export function formatHitRate(rate: number | null): string {
  if (rate === null) return "—";
  return `${Math.round(rate * 100)}%`;
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

/** "03:30" (+ optional weekly day) → the `schedule.cron` string. */
export function cronFromTime(time: string, weekly = false): string {
  const [hoursRaw, minutesRaw] = time.split(":");
  const hours = Number(hoursRaw);
  const minutes = Number(minutesRaw);
  const safeHours =
    Number.isInteger(hours) && hours >= 0 && hours <= 23 ? hours : 3;
  const safeMinutes =
    Number.isInteger(minutes) && minutes >= 0 && minutes <= 59 ? minutes : 30;
  return `${safeMinutes} ${safeHours} * * ${weekly ? "0" : "*"}`;
}

/**
 * Whether a cron string is one the simple Run at + Nightly/Weekly presets can round-trip losslessly.
 * The presets ONLY ever emit nightly (`* * *` dow `*`) or Sunday-weekly (dow `0`) — cronFromTime
 * writes `0` for weekly and timeFromCron collapses any non-`*` dow to "weekly", so `0` is the sole
 * weekday they can represent. Every other cron (a non-Sunday weekday, steps, ranges, lists, specific
 * months) would be silently flattened, so it must open as-is in Custom mode instead.
 */
export function isPresetCron(cron: string): boolean {
  const parts = cron.trim().split(/\s+/);
  if (parts.length !== 5) return false;
  const [minuteField, hourField, domField, monthField, dowField] = parts as [
    string,
    string,
    string,
    string,
    string,
  ];
  const minutes = Number(minuteField);
  const hours = Number(hourField);
  const clockOk =
    Number.isInteger(minutes) &&
    minutes >= 0 &&
    minutes <= 59 &&
    Number.isInteger(hours) &&
    hours >= 0 &&
    hours <= 23;
  const dailyFields = domField === "*" && monthField === "*";
  const dowOk = dowField === "*" || dowField === "0"; // only nightly or Sunday-weekly round-trip
  return clockOk && dailyFields && dowOk;
}

/** Best-effort inverse of cronFromTime; falls back to 03:30 nightly. */
export function timeFromCron(cron: string): { time: string; weekly: boolean } {
  const parts = cron.trim().split(/\s+/);
  const minutes = Number(parts[0]);
  const hours = Number(parts[1]);
  if (
    parts.length === 5 &&
    Number.isInteger(minutes) &&
    minutes >= 0 &&
    minutes <= 59 &&
    Number.isInteger(hours) &&
    hours >= 0 &&
    hours <= 23
  ) {
    return {
      time: `${String(hours).padStart(2, "0")}:${String(minutes).padStart(2, "0")}`,
      weekly: parts[4] !== "*",
    };
  }
  return { time: "03:30", weekly: false };
}

/** Row-name templates render {top_seed} from each user's history nightly. */
export function renderRowName(template: string, topSeed = "Fargo"): string {
  return template.replaceAll("{top_seed}", topSeed);
}
