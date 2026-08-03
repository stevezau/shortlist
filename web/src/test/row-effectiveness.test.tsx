import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { RowEffectivenessPanel } from "@/components/rows/row-effectiveness";
import type { RowEffectiveness } from "@/lib/types";

const BASE: RowEffectiveness = {
  delivered: 12,
  watched: 3,
  runs: 7,
  first_delivered_at: "2026-06-01T00:00:00Z",
  last_delivered_at: "2026-08-01T00:00:00Z",
  matured_days: 30,
  matured: null,
  per_library: [],
};

function panel(data: Partial<RowEffectiveness> = {}) {
  render(
    <MemoryRouter>
      <RowEffectivenessPanel
        data={{ ...BASE, ...data }}
        isLoading={false}
        rowSlug="picked for you"
      />
    </MemoryRouter>,
  );
}

describe("RowEffectivenessPanel", () => {
  it("links the Runs tile to the row's own history, by SLUG", () => {
    // It linked to `/runs?row=<id>` while the Runs page filters on the SLUG, so the one link out of
    // this panel matched no row at all — an empty list with no hint as to why.
    panel();

    const link = screen.getByRole("link", { name: /Runs/i });
    expect(link.getAttribute("href")).toBe("/runs?row=picked%20for%20you");
  });

  it("shows four tiles before a cohort has matured", () => {
    panel();

    // Anchored: an unanchored /Watched/ also matches the hint "of judged picks watched".
    for (const label of [/^Delivered$/, /^Watched$/, /^Runs$/, /^Last built$/]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    expect(screen.getByText(/Too early for a score/i)).toBeTruthy();
  });

  it("still shows four tiles once a cohort matures, and moves the judged count into the note", () => {
    panel({
      matured: {
        delivered: 9,
        watched: 4,
        rate: 0.44,
        cohort_to: "2026-07-05T00:00:00Z",
      },
    });

    for (const label of [/^Hit rate$/, /^Watched$/, /^Delivered$/, /^Runs$/]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    expect(screen.getByText(/44%/)).toBeTruthy();
    expect(screen.getByText(/Judged on 9 picks/i)).toBeTruthy();
  });

  it("says so plainly when the row has never delivered, rather than showing zeros", () => {
    panel({ first_delivered_at: null, delivered: 0, watched: 0, runs: 0 });

    expect(screen.getByText(/hasn’t delivered anything yet/i)).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Runs/i })).toBeNull();
  });
});
