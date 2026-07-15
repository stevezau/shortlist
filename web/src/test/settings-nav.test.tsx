import { render, screen } from "@testing-library/react";
import { Cable, SlidersHorizontal, TriangleAlert } from "lucide-react";
import { describe, expect, it } from "vitest";

import { SettingsNav } from "@/components/settings/settings-nav";

const SECTIONS = [
  { id: "connections", label: "Connections", icon: Cable },
  { id: "advanced", label: "Advanced", icon: SlidersHorizontal },
  { id: "danger", label: "Danger zone", icon: TriangleAlert },
];

describe("SettingsNav", () => {
  it("lists every section as an anchor link that jumps to its id", () => {
    render(<SettingsNav sections={SECTIONS} />);
    for (const { id, label } of SECTIONS) {
      const link = screen.getByRole("link", { name: label });
      expect(link.getAttribute("href")).toBe(`#${id}`);
    }
  });

  it("marks the first section active when nothing is scrolled into view yet", () => {
    // jsdom has no IntersectionObserver, so the scroll-spy degrades to "first section active".
    render(<SettingsNav sections={SECTIONS} />);
    expect(
      screen
        .getByRole("link", { name: "Connections" })
        .getAttribute("aria-current"),
    ).toBe("true");
    expect(
      screen
        .getByRole("link", { name: "Advanced" })
        .getAttribute("aria-current"),
    ).toBeNull();
  });
});
