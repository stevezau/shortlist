import { describe, expect, it } from "vitest";

import { blankInput, hasUnsavedChanges, OVER_TIME_DEFAULTS, toInput } from "@/lib/collections";
import type { Collection } from "@/lib/types";

function row(patch: Partial<Collection> = {}): Collection {
  return {
    id: 9,
    slug: "twist",
    name: "Twist",
    theme_id: 5,
    theme_mode: "fixed",
    explore_brief: "",
    theme_days: null,
    refresh_share: null,
    repeat_cooldown_days: null,
    avoid_rows: null,
    ai_instructions: { mode: "default", text: "" },
    ...patch,
  } as unknown as Collection;
}

describe("over-time fields in the row form", () => {
  it("round-trips a default collection to the defaults the server stores", () => {
    const input = toInput(row());

    expect(input).toMatchObject(OVER_TIME_DEFAULTS);
    expect(OVER_TIME_DEFAULTS).toEqual({
      theme_mode: "fixed",
      explore_brief: "",
      theme_days: null,
      refresh_share: null,
      repeat_cooldown_days: null,
      avoid_rows: null,
    });
    expect(hasUnsavedChanges(input, row())).toBe(false);
  });

  it("starts a new row at the defaults", () => {
    expect(blankInput()).toMatchObject(OVER_TIME_DEFAULTS);
  });

  it("maps each saved field to its form field one to one", () => {
    const input = toInput(
      row({
        theme_mode: "explore",
        explore_brief: "slow-burn mysteries",
        theme_days: 14,
        refresh_share: 0.5,
        repeat_cooldown_days: 60,
        avoid_rows: ["because-you-watched"],
      }),
    );

    expect(input.theme_mode).toBe("explore");
    expect(input.explore_brief).toBe("slow-burn mysteries");
    expect(input.theme_days).toBe(14);
    expect(input.refresh_share).toBe(0.5);
    expect(input.repeat_cooldown_days).toBe(60);
    expect(input.avoid_rows).toEqual(["because-you-watched"]);
  });

  it("sends null for the cooldown once it is switched off", () => {
    const on = toInput(row({ repeat_cooldown_days: 60 }));
    const off = { ...on, repeat_cooldown_days: null };

    expect(hasUnsavedChanges(off, row({ repeat_cooldown_days: 60 }))).toBe(true);
    expect(off.repeat_cooldown_days).toBeNull();
  });

  it("reads a row that predates the fields as the defaults", () => {
    const old = { id: 1, theme_id: null, ai_instructions: { mode: "default", text: "" } } as unknown as Collection;

    expect(toInput(old)).toMatchObject(OVER_TIME_DEFAULTS);
  });
});
