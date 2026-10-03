/** Local midnight, so "tomorrow" is the owner's calendar day rather than 24 hours from now. */
function midnight(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
}

/**
 * "Today 02:30", "Tomorrow 02:30", "Yesterday 02:30", else "Fri 9 Oct 02:30".
 *
 * Days are counted between local midnights, not in 24-hour blocks: a run at 02:30 seen at 20:00 is
 * "Tomorrow", which is what the owner means by it. `now` is injectable for tests.
 */
export function dayTime(iso: string, now: Date = new Date()): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "—";
  const time = date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  const days = Math.round((midnight(date) - midnight(now)) / 86_400_000);
  if (days === 0) return `Today ${time}`;
  if (days === 1) return `Tomorrow ${time}`;
  if (days === -1) return `Yesterday ${time}`;
  const day = date.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
  return `${day} ${time}`;
}
