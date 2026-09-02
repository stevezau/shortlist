/**
 * "When it appears" — which days of the week a row is on people's Home (issue #102).
 *
 * ISO weekdays throughout (1 = Monday .. 7 = Sunday), matching the API and `engine.rows.row_is_shown`.
 * An EMPTY list means every day, which is what every row carries after migration 0088 — there is
 * deliberately no way to spell "never", because switching the row off already means that.
 */

export type ShowDays = number[];

export const DAY_CHIPS: { iso: number; short: string; long: string }[] = [
  { iso: 1, short: "Mon", long: "Monday" },
  { iso: 2, short: "Tue", long: "Tuesday" },
  { iso: 3, short: "Wed", long: "Wednesday" },
  { iso: 4, short: "Thu", long: "Thursday" },
  { iso: 5, short: "Fri", long: "Friday" },
  { iso: 6, short: "Sat", long: "Saturday" },
  { iso: 7, short: "Sun", long: "Sunday" },
];

/**
 * A Date's ISO weekday, 1 (Monday) .. 7 (Sunday).
 *
 * `Date.getDay()` calls Sunday 0 and the API takes ISO 1-7, so sending getDay() straight through
 * would store a day that matches nothing — a row that silently never appears on Sundays, with no
 * error to notice. This is the only place that conversion happens.
 */
export function isoWeekday(when: Date): number {
  const js = when.getDay();
  return js === 0 ? 7 : js;
}

/**
 * Deliberately absent: an `isShownToday(days)` that reads `new Date()`.
 *
 * Days turn over on the SERVER's clock — the one the midnight job and Plex follow — so a badge
 * derived from the viewer's clock can say "Hidden today" for a row Plex is showing, for as long as
 * the two timezones differ. The API answers it instead, as `Collection.shown_today`.
 */

function inWeekOrder(days: ShowDays): typeof DAY_CHIPS {
  return DAY_CHIPS.filter((chip) => days.includes(chip.iso));
}

/** "Mon, Wed, Fri" — or "Every day" when the schedule does not narrow anything. */
export function showDaysSummary(days: ShowDays): string {
  const chosen = inWeekOrder(days);
  if (chosen.length === 0 || chosen.length === 7) return "Every day";
  return chosen.map((chip) => chip.short).join(", ");
}

/** "Monday, Wednesday and Friday" — an English list, not a comma-joined array. */
function englishList(names: string[]): string {
  if (names.length <= 1) return names.join("");
  return `${names.slice(0, -1).join(", ")} and ${names[names.length - 1]}`;
}

/**
 * The line under the control, naming the days the row is HIDDEN.
 *
 * Deliberately says the hidden days out loud rather than only the shown ones: "why is my row not
 * there today" is the question this feature creates, and answering it in the editor is cheaper than
 * answering it in an issue. Empty for an always-on row — there is nothing to explain.
 */
export function showDaysSentence(days: ShowDays): string {
  const shown = inWeekOrder(days);
  if (shown.length === 0 || shown.length === 7) return "";
  const hidden = DAY_CHIPS.filter((chip) => !days.includes(chip.iso));
  return (
    `Shows on ${englishList(shown.map((c) => c.long))}. ` +
    `Hidden on ${englishList(hidden.map((c) => c.long))} — ` +
    `it keeps its titles, so it comes straight back.`
  );
}
