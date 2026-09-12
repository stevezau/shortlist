/**
 * The sidebar's support block: a star and a coffee, side by side.
 *
 * Two doors because they reach different people. A star is free and needs only a GitHub account; a
 * coffee is a guest checkout, for the Plex owner who has no GitHub account at all — which is most of
 * them, and exactly who a GitHub Sponsors link shut out.
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { SupportLinks } from "@/components/layout/app-shell";

describe("SupportLinks", () => {
  it("links the star to the repo and the coffee to Ko-fi", () => {
    render(<SupportLinks />);

    const star = screen.getByRole("link", { name: /star on github/i });
    const coffee = screen.getByRole("link", { name: /buy me a coffee/i });

    expect(star.getAttribute("href")).toBe("https://github.com/stevezau/shortlist");
    expect(coffee.getAttribute("href")).toBe("https://ko-fi.com/stevezau");
  });

  it("opens both in a new tab without handing the app's window to the other site", () => {
    render(<SupportLinks />);

    for (const link of screen.getAllByRole("link")) {
      expect(link.getAttribute("target")).toBe("_blank");
      expect(link.getAttribute("rel")).toContain("noopener");
    }
  });
});
