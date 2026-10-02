import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SeasonYearStrip } from "@/components/rows/seasons/season-year-strip";
import { seasonDate } from "@/lib/seasons";

import { CHRISTMAS, HALLOWEEN, THANKSGIVING } from "./season-fixtures";

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

  it("leaves the drawing to sighted readers: the bars are hidden from screen readers", () => {
    const { container } = render(
      <SeasonYearStrip seasons={[THANKSGIVING, CHRISTMAS]} leadDays={30} afterDays={0} today="2026-10-02" />,
    );
    const drawing = container.querySelector("[data-year-strip]");
    expect(drawing).not.toBeNull();
    expect(drawing).toHaveAttribute("aria-hidden", "true");
  });
});
