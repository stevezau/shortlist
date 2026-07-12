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
