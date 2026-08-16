import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { RowEffectivenessPanel } from "@/components/rows/row-effectiveness";
import type { RowEffectiveness } from "@/lib/types";

const BASE: RowEffectiveness = {
  delivered: 12,
  watched: 3,
  finished: 1,
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
    for (const label of [
      /^Delivered$/,
      /^Watched$/,
      /^Runs$/,
      /^Last built$/,
    ]) {
      expect(screen.getByText(label)).toBeTruthy();
    }
    expect(screen.getByText(/Too early for a score/i)).toBeTruthy();
  });

  it("still shows four tiles once a cohort matures, and moves the judged count into the note", () => {
    panel({
      matured: {
        delivered: 9,
        watched: 4,
        finished: 2,
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

  it("shows the finished count in the MATURED panel, not only before a cohort exists", () => {
    // Caught live, not by a test: the finished hint was added to the "too early to judge" branch
    // only, so the state a row spends its whole life in never showed it. Both branches render a
    // Watched tile and they are separate JSX — asserting one proves nothing about the other.
    panel({
      matured: {
        delivered: 9,
        watched: 4,
        finished: 2,
        rate: 0.44,
        cohort_to: "2026-07-05T00:00:00Z",
      },
    });

    expect(screen.getByText(/2 finished/)).toBeTruthy();
  });

  it("splits each library's landings into finished and still-going", () => {
    // The case this exists for: both libraries land the same share, so on the old bar the row read
    // as performing identically in each. It doesn't — the movie half gets finished, the TV half
    // gets sampled, because a series is credited on its first episode.
    panel({
      matured: {
        delivered: 20,
        watched: 8,
        finished: 5,
        rate: 0.4,
        cohort_to: "2026-07-05T00:00:00Z",
      },
      per_library: [
        {
          library: "Movies",
          delivered: 10,
          watched: 4,
          finished: 4,
          rate: 0.4,
        },
        {
          library: "TV Shows",
          delivered: 10,
          watched: 4,
          finished: 1,
          rate: 0.4,
        },
      ],
    });

    expect(screen.getByText(/4 finished \(100%\)/)).toBeTruthy();
    expect(screen.getByText(/1 finished \(25%\)/)).toBeTruthy();
  });

  it("says so plainly when the row has never delivered, rather than showing zeros", () => {
    panel({ first_delivered_at: null, delivered: 0, watched: 0, runs: 0 });

    expect(screen.getByText(/hasn’t delivered anything yet/i)).toBeTruthy();
    expect(screen.queryByRole("link", { name: /Runs/i })).toBeNull();
  });
});
