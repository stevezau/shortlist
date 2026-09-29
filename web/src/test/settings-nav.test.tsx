import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { SETTINGS_SECTIONS } from "@/components/settings/sections";
import { SettingsSubNav } from "@/components/settings/settings-nav";

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <SettingsSubNav />
    </MemoryRouter>,
  );
}

describe("SettingsSubNav", () => {
  it("tracks the section being read after scrolling past the URL anchor", () => {
    let recommendationsTop = 1500;
    let connectionsTop = 0;
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const top = this.id === "connections" ? connectionsTop : recommendationsTop;
      return { top, bottom: top + 1000, left: 0, right: 800, width: 800, height: 1000, x: 0, y: top, toJSON() {} };
    });
    render(<MemoryRouter initialEntries={["/settings#connections"]}><SettingsSubNav /><section id="connections" /><section id="recommendations" /></MemoryRouter>);
    expect(screen.getByRole("link", { name: "Connections" })).toHaveAttribute("aria-current", "true");
    connectionsTop = -1600;
    recommendationsTop = -100;
    fireEvent.scroll(window);
    expect(screen.getByRole("link", { name: "Finding titles" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: "Connections" })).not.toHaveAttribute("aria-current");
    vi.restoreAllMocks();
  });

  it("lists every settings section as an anchor link that jumps to its id, on /settings", () => {
    renderAt("/settings");
    for (const { id, label } of SETTINGS_SECTIONS) {
      const link = screen.getByRole("link", { name: label });
      expect(link.getAttribute("href")).toBe(`/settings#${id}`);
    }
  });

  it("marks the first section active when nothing is scrolled into view yet", () => {
    // jsdom has no IntersectionObserver, so the scroll-spy degrades to "first section active".
    renderAt("/settings");
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

  it("renders nothing when NOT on the settings page (it lives in the shared sidebar)", () => {
    renderAt("/rows");
    expect(screen.queryByRole("link", { name: "Connections" })).toBeNull();
  });
});
