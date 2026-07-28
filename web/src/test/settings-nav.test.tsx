import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

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
  it("lists every settings section as an anchor link that jumps to its id, on /settings", () => {
    renderAt("/settings");
    for (const { id, label } of SETTINGS_SECTIONS) {
      const link = screen.getByRole("link", { name: label });
      expect(link.getAttribute("href")).toBe(`#${id}`);
    }
  });

  it("marks the first section active on a bare /settings", () => {
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

  it("marks the section named by the hash active, so deep links land on their own pane", () => {
    renderAt("/settings#recommendations");
    expect(
      screen
        .getByRole("link", { name: "Finding titles" })
        .getAttribute("aria-current"),
    ).toBe("true");
    expect(
      screen
        .getByRole("link", { name: "Connections" })
        .getAttribute("aria-current"),
    ).toBeNull();
  });

  it("falls back to the first section for a hash naming no section", () => {
    renderAt("/settings#schedules");
    expect(
      screen
        .getByRole("link", { name: "Connections" })
        .getAttribute("aria-current"),
    ).toBe("true");
  });

  it("renders nothing when NOT on the settings page (it lives in the shared sidebar)", () => {
    renderAt("/rows");
    expect(screen.queryByRole("link", { name: "Connections" })).toBeNull();
  });
});
