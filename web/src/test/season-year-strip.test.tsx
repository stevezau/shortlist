import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SeasonYearStrip } from "@/components/rows/seasons/season-year-strip";
import { seasonDate } from "@/lib/seasons";

import { CHRISTMAS, FEBRUARY_SPOTLIGHT, HALLOWEEN, THANKSGIVING } from "./season-fixtures";

describe("SeasonYearStrip", () => {
  it("says which seasons overlap, on which days, and which one wins", () => {
    // Thanksgiving (US) runs 12-26 Nov on its own 14 days; Christmas, a built-in on the row's 30-day
    // lead, starts 25 Nov.
    render(<SeasonYearStrip seasons={[THANKSGIVING, CHRISTMAS]} leadDays={30} afterDays={0} today="2026-10-02" />);
    expect(
      screen.getByText(
        `Thanksgiving and Christmas overlap on ${seasonDate("2026-11-25")} – ${seasonDate("2026-11-26")}: the nearer date wins.`,
      ),
    ).toBeInTheDocument();
  });

  it("says so when nothing overlaps", () => {
    render(<SeasonYearStrip seasons={[HALLOWEEN, CHRISTMAS]} leadDays={30} afterDays={0} today="2026-10-02" />);
    expect(screen.getByText(/No seasons overlap\./)).toBeInTheDocument();
  });

  it("shows a full month's server boundaries and explains a dated occasion winning a tie", () => {
    const october = {
      ...FEBRUARY_SPOTLIGHT,
      name: "October spotlight",
      rule: { ...FEBRUARY_SPOTLIGHT.rule, month: 10 },
      next_dates: ["2026-10-31", "2027-10-31"],
      next_windows: [
        { start: "2026-10-01", end: "2026-10-31" },
        { start: "2027-10-01", end: "2027-10-31" },
      ],
    };
    render(<SeasonYearStrip seasons={[october, HALLOWEEN]} leadDays={7} afterDays={0} today="2026-10-02" />);
    expect(screen.getByRole("img")).toHaveAccessibleName(
      `When this row shows each season: October spotlight, ${seasonDate("2026-10-01")} – ${seasonDate("2026-10-31")}; ` +
        `Halloween, ${seasonDate("2026-10-24")} – ${seasonDate("2026-10-31")}. Today is ${seasonDate("2026-10-02")}.`,
    );
    expect(screen.getByText(/When dates tie, a dated occasion takes priority over a full-month season\./)).toBeInTheDocument();
  });

  it("is one labelled image to a screen reader: each season's window, and today", () => {
    render(<SeasonYearStrip seasons={[THANKSGIVING, CHRISTMAS]} leadDays={30} afterDays={0} today="2026-10-02" />);
    expect(screen.getByRole("img")).toHaveAccessibleName(
      `When this row shows each season: Thanksgiving, ${seasonDate("2026-11-12")} – ${seasonDate("2026-11-26")}; ` +
        `Christmas, ${seasonDate("2026-11-25")} – ${seasonDate("2026-12-25")}. Today is ${seasonDate("2026-10-02")}.`,
    );
  });

  it("names each lane, so two seasons with one emoji can be told apart", () => {
    const mothers = { ...THANKSGIVING, slug: "mothers-day", name: "Mother's Day", emoji: "💐" };
    const mothering = { ...THANKSGIVING, slug: "mothering-sunday", name: "Mothering Sunday", emoji: "💐" };
    render(<SeasonYearStrip seasons={[mothering, mothers]} leadDays={30} afterDays={0} today="2026-10-02" />);
    expect(screen.getByText("💐 Mothering Sunday")).toBeInTheDocument();
    expect(screen.getByText("💐 Mother's Day")).toBeInTheDocument();
    expect(screen.getByText("Today")).toBeInTheDocument();
  });
});
