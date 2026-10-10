import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PickList } from "@/components/pick-list";
import type { Pick } from "@/lib/types";

function pick(rank: number, title: string): Pick {
  return {
    rank,
    title,
    // Required since posters landed — the artwork is proxied from the PMS by rating key, which is
    // the one identifier all four Pick construction sites carry.
    rating_key: 1000 + rank,
    reason: "because you watched Fargo",
    seed_title: null,
    sources: [],
    affinity: null,
  };
}

describe("PickList", () => {
  it("renders every rank neutral and tabular, because amber is reserved for the one action", () => {
    render(
      <PickList
        picks={[pick(1, "Heat"), pick(2, "Sicario"), pick(3, "Fargo")]}
      />,
    );

    for (const rank of ["#1", "#2", "#3"]) {
      const cls = screen.getByText(rank).className;
      expect(cls).not.toMatch(/text-primary/);
      expect(cls).toMatch(/text-muted-foreground/);
      expect(cls).toMatch(/tabular-nums/);
    }
  });

  it("never says a rewatch was inspired by itself", () => {
    // A watch-it-again pick is its own seed (so a {top_seed} name can render), and its reason already
    // says why it is there — "inspired by Heat" under Heat is noise.
    render(
      <PickList
        picks={[
          {
            ...pick(1, "Heat"),
            reason: "Last watched March 2024",
            seed_title: "Heat",
            sources: ["history"],
          },
        ]}
      />,
    );

    expect(screen.queryByText(/inspired by/)).not.toBeInTheDocument();
  });

  it("still orders by rank when picks arrive out of order", () => {
    render(<PickList picks={[pick(3, "Fargo"), pick(1, "Heat")]} />);

    const ranks = screen.getAllByText(/^#\d$/).map((el) => el.textContent);
    expect(ranks).toEqual(["#1", "#3"]);
  });

  it("gives the Show all text button a tap target beyond its 19px of text, on touch screens only", () => {
    const picks = Array.from({ length: 12 }, (_, n) => pick(n + 1, `Title ${n + 1}`));
    render(<PickList picks={picks} collapseAfter={5} />);

    const classes = screen.getByRole("button", { name: /Show all/ }).className.split(" ");
    expect(classes).toContain("[@media(pointer:coarse)]:before:h-11");
    expect(classes).not.toContain("relative");
  });
});
