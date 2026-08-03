/**
 * Cron helpers for the schedule pickers: read plain English into a cron expression, and read a cron
 * expression back out as a sentence.
 *
 * Owners are not sysadmins. The "Custom" schedule boxes used to take a bare five-field cron with no
 * label saying what it was and no feedback on what it meant, so a typo saved happily and the
 * scheduler silently fell back to its built-in default (`_resolve_watch_cron` and friends in
 * server/scheduler.py log a warning nobody reads). `describeCron` closes that loop before the save,
 * and `parseNaturalSchedule` means most people never have to write cron at all.
 */

const DAY_NAMES = [
  "Sunday",
  "Monday",
  "Tuesday",
  "Wednesday",
  "Thursday",
  "Friday",
  "Saturday",
];

/** Written-out weekday → cron day-of-week number. Each pattern also accepts the plural ("mondays"). */
const DAY_ALIASES: Array<[RegExp, number]> = [
  [/\bsun(day)?s?\b/, 0],
  [/\bmon(day)?s?\b/, 1],
  [/\btue(s|sday)?s?\b/, 2],
  [/\bwed(nesday)?s?\b/, 3],
  [/\bthu(r|rs|rsday)?s?\b/, 4],
  [/\bfri(day)?s?\b/, 5],
  [/\bsat(urday)?s?\b/, 6],
];

const MONTH_TOKENS = [
  "jan",
  "feb",
  "mar",
  "apr",
  "may",
  "jun",
  "jul",
  "aug",
  "sep",
  "oct",
  "nov",
  "dec",
];
const DOW_TOKENS = ["sun", "mon", "tue", "wed", "thu", "fri", "sat"];

/** Per-field bounds and allowed names, in cron field order. Day-of-week allows 7 as a second Sunday,
 *  and both name lists are accepted because APScheduler's `CronTrigger.from_crontab` accepts them. */
const FIELDS: Array<{ min: number; max: number; names: string[] }> = [
  { min: 0, max: 59, names: [] }, // minute
  { min: 0, max: 23, names: [] }, // hour
  { min: 1, max: 31, names: [] }, // day of month
  { min: 1, max: 12, names: MONTH_TOKENS }, // month
  { min: 0, max: 7, names: DOW_TOKENS }, // day of week
];

function isValidField(
  field: string,
  { min, max, names }: { min: number; max: number; names: string[] },
): boolean {
  if (!field) return false;
  return field.split(",").every((part) => {
    const segments = part.split("/");
    if (segments.length > 2) return false;
    const [range = "", step] = segments;
    if (step !== undefined && (!/^\d+$/.test(step) || Number(step) < 1)) {
      return false;
    }
    if (range === "*") return true;
    return range.split("-").length <= 2
      ? range
          .split("-")
          .every(
            (bound) =>
              names.includes(bound.toLowerCase()) ||
              (/^\d+$/.test(bound) &&
                Number(bound) >= min &&
                Number(bound) <= max),
          )
      : false;
  });
}

/** Whether a string is a five-field cron expression the scheduler will accept. */
export function isValidCron(expression: string): boolean {
  const fields = expression.trim().split(/\s+/);
  return (
    fields.length === FIELDS.length &&
    FIELDS.every((spec, i) => isValidField(fields[i] ?? "", spec))
  );
}

type TimeOfDay = { hour: number; minute: number };

function parseTimeOfDay(text: string): TimeOfDay | null {
  if (/\b(noon|midday)\b/.test(text)) return { hour: 12, minute: 0 };
  if (/\bmidnight\b/.test(text)) return { hour: 0, minute: 0 };
  // An explicit "at …" wins, so a number that belongs to an interval ("every 4 hours") is never
  // mistaken for a clock time. Without it, only unambiguous forms count: "9pm" or "21:30".
  const match =
    text.match(/\bat\s+(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?/) ??
    text.match(/\b(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)\b/) ??
    text.match(/\b(\d{1,2}):(\d{2})\b/);
  if (!match) return null;
  let hour = Number(match[1]);
  const minute = match[2] ? Number(match[2]) : 0;
  if (match[3] === "pm" && hour < 12) hour += 12;
  if (match[3] === "am" && hour === 12) hour = 0;
  if (hour > 23 || minute > 59) return null;
  return { hour, minute };
}

function parseDayOfWeek(text: string): string | null {
  if (/\bweekdays?\b/.test(text)) return "1-5";
  if (/\bweekends?\b/.test(text)) return "0,6";
  for (const [pattern, day] of DAY_ALIASES) {
    if (pattern.test(text)) return String(day);
  }
  return null;
}

/** "17 past" / "at 17 minutes past" → 17, so "every 4 hours at 17 past" keeps its offset. */
function minutesPast(text: string): number {
  const match = text.match(/\b(\d{1,2})\s*(?:minutes?|mins?)?\s+past\b/);
  const value = match ? Number(match[1]) : 0;
  return value <= 59 ? value : 0;
}

/**
 * Read a schedule written however the owner thinks about it and return a cron expression.
 *
 * Understands intervals ("every 15 minutes", "every 4 hours at 17 past", "hourly"), clock times
 * ("every day at 3:30am", "nightly at 4am") and weekdays ("mondays at 9pm", "weekdays at 6am").
 * An input that is already valid cron passes straight through. Returns null when nothing is
 * recognisable, so the caller can say so rather than saving a guess.
 */
export function parseNaturalSchedule(input: string): string | null {
  const text = input.trim().toLowerCase().replace(/\s+/g, " ");
  if (!text) return null;
  if (isValidCron(text)) return text;

  const everyMinutes = text.match(/\bevery\s+(\d+)\s*(?:minutes?|mins?|m)\b/);
  if (everyMinutes) {
    const n = Number(everyMinutes[1]);
    if (n < 1 || n > 59) return null;
    return n === 1 ? "* * * * *" : `*/${n} * * * *`;
  }
  if (/\bevery minute\b/.test(text)) return "* * * * *";

  const everyHours = text.match(/\bevery\s+(\d+)\s*(?:hours?|hrs?|h)\b/);
  if (everyHours) {
    const n = Number(everyHours[1]);
    if (n < 1 || n > 23) return null;
    const past = minutesPast(text);
    return n === 1 ? `${past} * * * *` : `${past} */${n} * * *`;
  }
  if (/\bhourly\b/.test(text) || /\bevery hour\b/.test(text)) {
    return `${minutesPast(text)} * * * *`;
  }
  if (/\btwice\s+(?:a|per|each)\s+day\b/.test(text)) return "0 */12 * * *";

  // Anything left is a clock time, optionally pinned to a weekday. With no time given, midnight is
  // the literal reading of "every Monday" — the caller shows the description back, so it self-corrects.
  const { hour, minute } = parseTimeOfDay(text) ?? { hour: 0, minute: 0 };
  const dow = parseDayOfWeek(text);
  if (dow !== null) return `${minute} ${hour} * * ${dow}`;
  if (/\b(everyday|every day|daily|nightly|each day)\b/.test(text)) {
    return `${minute} ${hour} * * *`;
  }
  return parseTimeOfDay(text) ? `${minute} ${hour} * * *` : null;
}

function formatClock(hour: number, minute: number): string {
  const suffix = hour < 12 ? "AM" : "PM";
  const twelve = hour % 12 === 0 ? 12 : hour % 12;
  return `${twelve}:${String(minute).padStart(2, "0")} ${suffix}`;
}

function describeDayOfWeek(dow: string): string | null {
  if (dow === "*") return "Every day";
  if (dow === "1-5") return "Every weekday";
  if (dow === "0,6" || dow === "6,0") return "Every Saturday and Sunday";
  const day = Number(dow);
  if (/^\d$/.test(dow) && day <= 7) return `Every ${DAY_NAMES[day % 7]}`;
  return null;
}

/**
 * The clock time a once-a-day cron fires, as "05:45" — short enough for a chip label.
 *
 * Returns null for anything that is not "every day at a fixed time", because a bare "05:45" would
 * misdescribe a weekday-only cron ("45 5 * * 1", Mondays) or a stepped one ("17 every-4-hours").
 * The caller falls back to a label with no time in it rather than showing a wrong one.
 */
export function dailyCronTime(expression: string): string | null {
  if (!isValidCron(expression)) return null;
  const [minute = "", hour = "", dom, month, dow] = expression
    .trim()
    .split(/\s+/);
  if (dom !== "*" || month !== "*" || dow !== "*") return null;
  if (!/^\d+$/.test(minute) || !/^\d+$/.test(hour)) return null;
  return `${hour.padStart(2, "0")}:${minute.padStart(2, "0")}`;
}

/**
 * A cron expression as a sentence — "Every 4 hours, at 17 minutes past".
 *
 * Covers the shapes the pickers can produce plus the common hand-written ones. Returns "" for an
 * expression it can't phrase (day-of-month rules, month lists, odd step combinations) rather than
 * risking a wrong description; the caller treats that as "valid, just not describable".
 */
export function describeCron(expression: string): string {
  if (!isValidCron(expression)) return "";
  const [minute = "", hour = "", dom, month, dow = ""] = expression
    .trim()
    .split(/\s+/);
  if (dom !== "*" || month !== "*") return "";

  const everyN = (field: string) =>
    /^\*\/\d+$/.test(field) ? Number(field.slice(2)) : null;

  if (dow === "*") {
    const minuteStep = everyN(minute);
    if (minute === "*" && hour === "*") return "Every minute";
    if (minuteStep && hour === "*") return `Every ${minuteStep} minutes`;

    const past = /^\d+$/.test(minute) ? Number(minute) : null;
    if (past === null) return "";
    const offset =
      past === 0 ? "" : `, at ${past} minute${past === 1 ? "" : "s"} past`;
    if (hour === "*") {
      return past === 0 ? "Every hour, on the hour" : `Every hour${offset}`;
    }
    const hourStep = everyN(hour);
    if (hourStep) return `Every ${hourStep} hours${offset}`;
  }

  if (!/^\d+$/.test(minute) || !/^\d+$/.test(hour)) return "";
  const day = describeDayOfWeek(dow);
  return day ? `${day} at ${formatClock(Number(hour), Number(minute))}` : "";
}
