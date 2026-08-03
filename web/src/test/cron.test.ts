import { describe, expect, it } from "vitest";

import {
  dailyCronTime,
  describeCron,
  isValidCron,
  parseNaturalSchedule,
} from "@/lib/cron";

describe("isValidCron", () => {
  it.each([
    "* * * * *",
    "0 3 * * *",
    "17 */4 * * *",
    "*/15 * * * *",
    "0 9 * * 1-5",
    "30 4 * * 0,6",
    "0 0 1 1 *",
    "0 12 * jan mon", // APScheduler accepts month/day names, so we must too
  ])("accepts %s", (expression) => {
    expect(isValidCron(expression)).toBe(true);
  });

  it.each([
    "",
    "0 3 * *", // four fields
    "0 3 * * * *", // six fields
    "60 3 * * *", // minute out of range
    "0 24 * * *", // hour out of range
    "0 3 * 13 *", // month out of range
    "0 3 * * 8", // day-of-week out of range
    "every 4 hours",
    "*/0 * * * *", // a zero step is not a schedule
    "0 3 * * mon-fri-sat", // three-part range
  ])("rejects %s", (expression) => {
    expect(isValidCron(expression)).toBe(false);
  });
});

describe("parseNaturalSchedule", () => {
  it.each([
    ["every 15 minutes", "*/15 * * * *"],
    ["every 5 min", "*/5 * * * *"],
    ["every minute", "* * * * *"],
    ["hourly", "0 * * * *"],
    ["every hour", "0 * * * *"],
    ["every 4 hours", "0 */4 * * *"],
    ["every 4 hours at 17 past", "17 */4 * * *"],
    ["every 2 hrs", "0 */2 * * *"],
    ["twice a day", "0 */12 * * *"],
    ["every day at 3:30am", "30 3 * * *"],
    ["nightly at 4am", "0 4 * * *"],
    ["daily at 21:15", "15 21 * * *"],
    ["every day at noon", "0 12 * * *"],
    ["at midnight", "0 0 * * *"],
    ["9pm", "0 21 * * *"],
    ["12am", "0 0 * * *"],
    ["mondays at 9pm", "0 21 * * 1"],
    ["every sunday at 4:30am", "30 4 * * 0"],
    ["weekdays at 6am", "0 6 * * 1-5"],
    ["weekends at 10am", "0 10 * * 0,6"],
  ])("reads %s as %s", (input, expected) => {
    expect(parseNaturalSchedule(input)).toBe(expected);
  });

  it("passes a cron expression through untouched", () => {
    expect(parseNaturalSchedule("17 */4 * * *")).toBe("17 */4 * * *");
  });

  it("is case- and whitespace-insensitive", () => {
    expect(parseNaturalSchedule("  Every  4   Hours ")).toBe("0 */4 * * *");
  });

  it.each([
    "",
    "   ",
    "sometime",
    "as often as possible",
    "every 90 minutes", // no cron field expresses this
    "every 40 hours",
  ])("returns null for %s rather than guessing", (input) => {
    expect(parseNaturalSchedule(input)).toBeNull();
  });

  it("does not read an interval's number as a clock time", () => {
    // "every 6 hours" must not become "6am" — the interval branch has to win.
    expect(parseNaturalSchedule("every 6 hours")).toBe("0 */6 * * *");
  });

  it("round-trips its own output through describeCron", () => {
    const cron = parseNaturalSchedule("every monday at 9:45pm");
    expect(cron).toBe("45 21 * * 1");
    expect(describeCron(cron ?? "")).toBe("Every Monday at 9:45 PM");
  });
});

describe("describeCron", () => {
  it.each([
    ["* * * * *", "Every minute"],
    ["*/15 * * * *", "Every 15 minutes"],
    ["0 * * * *", "Every hour, on the hour"],
    ["17 * * * *", "Every hour, at 17 minutes past"],
    ["1 * * * *", "Every hour, at 1 minute past"],
    ["0 */4 * * *", "Every 4 hours"],
    ["17 */4 * * *", "Every 4 hours, at 17 minutes past"],
    ["30 3 * * *", "Every day at 3:30 AM"],
    ["0 0 * * *", "Every day at 12:00 AM"],
    ["0 12 * * *", "Every day at 12:00 PM"],
    ["0 21 * * 1", "Every Monday at 9:00 PM"],
    ["30 4 * * 0", "Every Sunday at 4:30 AM"],
    ["30 4 * * 7", "Every Sunday at 4:30 AM"], // cron's second Sunday
    ["0 6 * * 1-5", "Every weekday at 6:00 AM"],
    ["0 10 * * 0,6", "Every Saturday and Sunday at 10:00 AM"],
  ])("describes %s as %s", (expression, expected) => {
    expect(describeCron(expression)).toBe(expected);
  });

  it.each([
    "not a cron",
    "0 3 * *",
    "0 3 1 * *", // day-of-month rules are valid but we don't phrase them
    "0 3 * 6 *", // month restriction, likewise
    "0 3 * * 1-3", // an arbitrary weekday range
  ])("returns empty for %s rather than a wrong sentence", (expression) => {
    expect(describeCron(expression)).toBe("");
  });
});

describe("dailyCronTime", () => {
  it("reads the clock time out of a once-a-day cron, zero-padded", () => {
    // The label on the "Built-in" chip: short enough for a chip, and the time the job runs at.
    expect(dailyCronTime("45 5 * * *")).toBe("05:45");
    expect(dailyCronTime("0 3 * * *")).toBe("03:00");
    expect(dailyCronTime("17 4 * * *")).toBe("04:17");
    expect(dailyCronTime("15 6 * * *")).toBe("06:15");
  });

  it("returns null for anything a bare clock time would misdescribe", () => {
    // "05:45" beside a Monday-only or every-five-hours cron would be a chip that lies about when the
    // job runs, which is worse than a chip with no time on it.
    expect(dailyCronTime("45 5 * * 1")).toBeNull();
    expect(dailyCronTime("17 */4 * * *")).toBeNull();
    expect(dailyCronTime("*/15 * * * *")).toBeNull();
    expect(dailyCronTime("0 3 1 * *")).toBeNull();
    expect(dailyCronTime("")).toBeNull();
    expect(dailyCronTime("nonsense")).toBeNull();
  });
});
