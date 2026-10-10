/** Local midnight, so "tomorrow" is the owner's calendar day rather than 24 hours from now. */
function midnight(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * A moment split into its day and its time: "Today" + "02:30", or "Fri 9 Oct" + "02:30".
 *
 * Days are counted between local midnights, not in 24-hour blocks: a run at 02:30 seen at 20:00 is
 * "Tomorrow", which is what the owner means by it. `now` is injectable for tests.
 */
export function dayAndTime(iso: string, now: Date = new Date()): { day: string; time: string } | null {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return null;
  const time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((midnight(date) - midnight(now)) / 86_400_000);
  if (days === 0) return { day: "Today", time };
  if (days === 1) return { day: "Tomorrow", time };
  if (days === -1) return { day: "Yesterday", time };
  const day = date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  return { day, time };
}

/** "Today 02:30", "Tomorrow 02:30", "Yesterday 02:30", else "Fri 9 Oct 02:30". */
export function dayTime(iso: string, now: Date = new Date()): string {
  const parts = dayAndTime(iso, now);
  return parts ? `${parts.day} ${parts.time}` : "—";
}

/** "02:30:04": a run is often seconds long, so a start → finish line carries seconds. */
export function clockTime(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

/** "2:30 AM" or "02:30": the time of day alone, hour unpadded, for a tooltip. */
export function timeOfDay(iso: string): string {
  return new Date(iso).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" });
}

/** "Fri 9 Oct 2026 02:30", or without the time: a date with its year, for a record that may be old. */
export function longDateTime(iso: string, withTime = true): string {
  return new Date(iso).toLocaleString(undefined, {
    weekday: "short",
    day: "numeric",
    month: "short",
    year: "numeric",
    ...(withTime ? { hour: "2-digit", minute: "2-digit" } : {}),
  });
}
