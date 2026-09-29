import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, useNavigate } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingsSections } from "@/components/settings/section-layout";
import { SettingsSubNav } from "@/components/settings/settings-nav";
import { SettingDisclosure } from "@/components/settings/setting-disclosure";

function HistoryButtons() { const navigate = useNavigate(); return <><button onClick={() => void navigate(-1)}>Back</button><button onClick={() => void navigate(1)}>Forward</button></>; }

describe("Continuous Settings sections", () => {
  const scrollIntoView = vi.fn();
  beforeEach(() => {
    localStorage.clear();
    scrollIntoView.mockClear();
    Element.prototype.scrollIntoView = scrollIntoView;
  });

  it.each(["/settings", "/settings?view=sections#recommendations", "/settings?view=all#danger"])("keeps every form visible despite legacy view preferences at %s", (path) => {
    localStorage.setItem("shortlist.settings.view", "sections");
    render(<MemoryRouter initialEntries={[path]}><SettingsSections content={{ connections: <input aria-label="Connection draft" />, recommendations: <p>Recommendation controls</p>, danger: <p>Removal controls</p> }} /></MemoryRouter>);
    expect(screen.getByLabelText("Connection draft")).toBeVisible();
    expect(screen.getByText("Recommendation controls")).toBeVisible();
    expect(screen.getByText("Removal controls")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Sections" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Show all" })).not.toBeInTheDocument();
  });

  it("repeats a sidebar or mobile jump when its hash is already in the address bar", async () => {
    render(<MemoryRouter initialEntries={["/settings#defaults"]}><SettingsSubNav /><SettingsSections content={{ defaults: <input aria-label="Naming draft" defaultValue="keep me" /> }} /></MemoryRouter>);
    scrollIntoView.mockClear();
    await userEvent.click(screen.getByRole("link", { name: "Row defaults" }));
    expect(scrollIntoView.mock.instances.at(-1)).toBe(document.getElementById("defaults"));
    scrollIntoView.mockClear();
    await userEvent.selectOptions(screen.getByLabelText("Settings section"), "defaults");
    expect(scrollIntoView.mock.instances.at(-1)).toBe(document.getElementById("defaults"));
    expect(screen.getByLabelText("Naming draft")).toHaveValue("keep me");
  });

  it("scrolls between sections and through browser history without hiding or resetting drafts", async () => {
    render(<MemoryRouter initialEntries={["/settings#connections"]}><HistoryButtons /><SettingsSections content={{ connections: <input aria-label="Draft" defaultValue="" />, recommendations: <p>Recommendations</p> }} /></MemoryRouter>);
    await userEvent.type(screen.getByLabelText("Draft"), "keep this");
    await userEvent.selectOptions(screen.getByLabelText("Settings section"), "recommendations");
    expect(scrollIntoView.mock.instances.at(-1)).toBe(document.getElementById("recommendations"));
    expect(screen.getByLabelText("Draft")).toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(scrollIntoView.mock.instances.at(-1)).toBe(document.getElementById("connections"));
    await userEvent.click(screen.getByRole("button", { name: "Forward" }));
    expect(scrollIntoView.mock.instances.at(-1)).toBe(document.getElementById("recommendations"));
    expect(screen.getByLabelText("Draft")).toHaveValue("keep this");
  });

  it("opens secondary controls before scrolling to an existing deep-linked field", () => {
    render(<MemoryRouter initialEntries={["/settings#min-history"]}><SettingsSections content={{ connections: <p>Connection controls</p>, recommendations: <SettingDisclosure title="More controls" value="Edit"><input id="min-history" aria-label="Deep linked history" /></SettingDisclosure> }} /></MemoryRouter>);
    expect(screen.getByLabelText("Deep linked history")).toBeVisible();
    expect(screen.getByText("Connection controls")).toBeVisible();
    expect(scrollIntoView.mock.instances.at(-1)).toBe(document.getElementById("min-history"));
  });
});
