import { fireEvent, render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { afterEach, describe, expect, it, vi } from "vitest";

import { SectionsWithJumps } from "@/components/settings/section-layout";
import {
  DEFAULTS_SECTIONS,
  resolveSettingsAnchor,
  searchSettings,
  settingsTabForHash,
} from "@/components/settings/sections";

describe("where an old Settings anchor lives now", () => {
  it.each([
    ["#connections", "connections"],
    ["#notifications", "connections"],
    ["#connection-trakt", "connections"],
    ["#recommendations", "defaults"],
    ["#recs-heading", "defaults"],
    ["#defaults", "defaults"],
    ["#row-defaults", "defaults"],
    ["#placement", "defaults"],
    ["#requests", "requests"],
    ["#watched-pct", "defaults"],
    ["#rating-source", "defaults"],
    ["#advanced", "system"],
    ["#api-access", "system"],
    ["#assistant-access", "system"],
    ["#assistant-access-heading", "system"],
    ["#danger", "system"],
    ["#danger-heading", "system"],
    ["", "connections"],
    ["#something-else", "connections"],
  ])("%s opens the %s tab", (hash, tab) => {
    expect(settingsTabForHash(hash)).toBe(tab);
  });

  it("renames the anchors the split renamed, and keeps every other id", () => {
    expect(resolveSettingsAnchor("recommendations")).toEqual({ tab: "defaults", anchor: "sources" });
    expect(resolveSettingsAnchor("defaults")).toEqual({ tab: "defaults", anchor: "row-defaults" });
    expect(resolveSettingsAnchor("min-history")).toEqual({ tab: "defaults", anchor: "min-history" });
    expect(resolveSettingsAnchor("danger")).toEqual({ tab: "system", anchor: "danger" });
    expect(resolveSettingsAnchor("not-a-setting")).toBeNull();
  });
});

describe("searchSettings", () => {
  it.each(["AI assistants", "MCP", "ChatGPT", "Claude", "Codex"])("finds assistant connections by %s", (query) => {
    expect(searchSettings(query)).toContainEqual(expect.objectContaining({ label: "AI assistants", to: "/assistant-access" }));
  });
  it("matches by name first, then by what it does", () => {
    const hits = searchSettings("trakt");
    expect(hits[0]?.label).toBe("Trakt");
    expect(hits.map((hit) => hit.label)).toContain("Title sources");
  });

  it("needs every word to match", () => {
    expect(searchSettings("run concurrency").map((hit) => hit.label)).toEqual(["Run concurrency"]);
    expect(searchSettings("concurrency zebra")).toEqual([]);
  });

  it("finds a setting that moved off Settings, and says where it went", () => {
    const [hit] = searchSettings("disabled users");
    expect(hit).toMatchObject({ label: "Disabled users see nothing", to: "/privacy", moved: true });
  });

  it("finds nothing for an empty query", () => {
    expect(searchSettings("   ")).toEqual([]);
  });
});

describe("the Defaults jump list", () => {
  afterEach(() => vi.restoreAllMocks());

  function renderJumps(path = "/settings/defaults") {
    return render(
      <MemoryRouter initialEntries={[path]}>
        <SectionsWithJumps tab="defaults" label="Defaults sections" sections={DEFAULTS_SECTIONS}>
          {DEFAULTS_SECTIONS.map((section) => (
            <section key={section.id} id={section.id} />
          ))}
        </SectionsWithJumps>
      </MemoryRouter>,
    );
  }

  it("offers a Jump to select for a phone, with every section in it", () => {
    renderJumps("/settings/defaults#refresh");
    const select = screen.getByRole("combobox", { name: "Defaults sections" });
    expect(Array.from(select.querySelectorAll("option")).map((option) => option.textContent)).toEqual(
      DEFAULTS_SECTIONS.map((section) => section.label),
    );
  });

  it("follows the section being read after scrolling past the address's anchor", () => {
    const tops: Record<string, number> = { sources: 0, refresh: 1500, "row-defaults": 3000, placement: 4500, requests: 6000 };
    vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
      const top = tops[this.id] ?? 0;
      return { top, bottom: top + 1000, left: 0, right: 800, width: 800, height: 1000, x: 0, y: top, toJSON() {} };
    });
    vi.spyOn(window, "requestAnimationFrame").mockImplementation((callback) => {
      callback(0);
      return 0;
    });
    renderJumps("/settings/defaults#sources");
    expect(screen.getByRole("link", { name: "Title sources" })).toHaveAttribute("aria-current", "true");
    Object.assign(tops, { sources: -1600, refresh: -100, "row-defaults": 1400 });
    fireEvent.scroll(window);
    expect(screen.getByRole("link", { name: "Refresh & variety" })).toHaveAttribute("aria-current", "true");
    expect(screen.getByRole("link", { name: "Title sources" })).not.toHaveAttribute("aria-current");
  });
});
