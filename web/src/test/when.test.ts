/** "Today 02:30" / "Tomorrow 02:30": a time the owner reads against their own calendar day. */
import { describe, expect, it } from "vitest";

import { dayTime } from "@/lib/when";

const NOW = new Date(2026, 9, 3, 20, 0);
const clock = (date: Date) => date.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });

describe("dayTime", () => {
  it("names today, tomorrow and yesterday by word", () => {
    const today = new Date(2026, 9, 3, 23, 15);
    const tomorrow = new Date(2026, 9, 4, 2, 30);
    const yesterday = new Date(2026, 9, 2, 2, 30);

    expect(dayTime(today.toISOString(), NOW)).toBe(`Today ${clock(today)}`);
    expect(dayTime(tomorrow.toISOString(), NOW)).toBe(`Tomorrow ${clock(tomorrow)}`);
    expect(dayTime(yesterday.toISOString(), NOW)).toBe(`Yesterday ${clock(yesterday)}`);
  });

  it("gives anything further off its date", () => {
    const later = new Date(2026, 9, 9, 2, 30);

    expect(dayTime(later.toISOString(), NOW)).toContain(clock(later));
    expect(dayTime(later.toISOString(), NOW)).not.toMatch(/Today|Tomorrow|Yesterday/);
  });

  it("says nothing it cannot back for an unreadable timestamp", () => {
    expect(dayTime("not a date", NOW)).toBe("—");
  });
});
