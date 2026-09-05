import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PickList } from "@/components/pick-list";
import type { Pick } from "@/lib/types";

function pick(rank: number, title: string): Pick {
  return {
    rank,
    title,
    reason: "because you watched Fargo",
    seed_title: null,
    sources: [],
    affinity: null,
  };
}

describe("PickList", () => {
  it("accents only the top pick's rank, so the colour says which one is the headline", () => {
    // Amber on every rank is chrome: #1 and #15 read identically, and the engine's own ordering —
    // the one fact this list exists to show — is carried by nothing but the digits.
    render(
      <PickList
        picks={[pick(1, "Heat"), pick(2, "Sicario"), pick(3, "Fargo")]}
      />,
    );

    expect(screen.getByText("#1").className).toMatch(/text-primary/);
    expect(screen.getByText("#2").className).not.toMatch(/text-primary/);
    expect(screen.getByText("#2").className).toMatch(/text-muted-foreground/);
    expect(screen.getByText("#3").className).not.toMatch(/text-primary/);
  });

  it("accents #1 even when the picks arrive out of rank order", () => {
    // The component sorts before it renders. If the accent were keyed on position rather than on
    // `rank`, an unsorted caller would highlight whatever happened to be first.
    render(<PickList picks={[pick(3, "Fargo"), pick(1, "Heat")]} />);

    expect(screen.getByText("#1").className).toMatch(/text-primary/);
    expect(screen.getByText("#3").className).not.toMatch(/text-primary/);
  });
});
