import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes, useLocation } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { Settings } from "@/lib/types";
import { SettingsPage } from "@/pages/settings";

const { getSettings, putSettings } = vi.hoisted(() => ({
  getSettings: vi.fn<() => Promise<Settings>>(),
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getSettings: () => getSettings(),
      putSettings: (values: Settings) => putSettings(values),
      getRuns: () => Promise.resolve([]),
      getApiToken: () => Promise.resolve({ active: false, created_at: null, last_used_at: null }),
      getAssistantStatus: () => Promise.resolve({ enabled: false, configuration_hint: "Configure the MCP URL." }),
      testConnection: () => Promise.resolve({ ok: true, message: "ok" }),
    },
  };
});

function Where() {
  const { pathname, hash } = useLocation();
  return <output aria-label="Address">{`${pathname}${hash}`}</output>;
}

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/settings/:tab?" element={<SettingsPage />} />
          <Route path="/privacy" element={<p>Privacy page</p>} />
          <Route path="/assistant-access" element={<p>AI assistant connections</p>} />
        </Routes>
        <Where />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const address = () => screen.getByLabelText("Address").textContent;
const visible = (node: Element) => node.closest("[hidden]") === null;

beforeEach(() => {
  getSettings.mockReset();
  getSettings.mockResolvedValue({});
  putSettings.mockClear();
  Element.prototype.scrollIntoView = vi.fn();
});

describe("Settings addresses", () => {
  it("opens Connections from a bare /settings", async () => {
    renderAt("/settings");
    expect(await screen.findByRole("tab", { name: "Connections", selected: true })).toBeVisible();
    await waitFor(() => expect(address()).toBe("/settings/connections"));
    expect(screen.getByRole("heading", { name: "Settings", level: 1 })).toBeVisible();
  });

  it.each([
    ["#connections", "connections", "/settings/connections#connections"],
    ["#notifications", "connections", "/settings/connections#notifications"],
    ["#recommendations", "defaults", "/settings/defaults#sources"],
    ["#defaults", "defaults", "/settings/defaults#row-defaults"],
    ["#placement", "defaults", "/settings/defaults#placement"],
    ["#requests", "requests", "/settings/requests#requests"],
    ["#min-history", "defaults", "/settings/defaults#min-history"],
    ["#advanced", "system", "/settings/system#advanced"],
    ["#api-access", "system", "/settings/system#api-access"],
    ["#assistant-access", "system", "/settings/system#assistant-access"],
    ["#danger", "system", "/settings/system#danger"],
  ])("lands an old /settings%s link on the right tab, scrolled to it", async (hash, tab, landed) => {
    renderAt(`/settings${hash}`);
    const label = tab[0]!.toUpperCase() + tab.slice(1);
    expect(await screen.findByRole("tab", { name: label, selected: true })).toBeVisible();
    await waitFor(() => expect(address()).toBe(landed));
    const target = document.getElementById(landed.split("#")[1]!);
    expect(target).not.toBeNull();
    expect(visible(target!)).toBe(true);
    await waitFor(() => expect(vi.mocked(Element.prototype.scrollIntoView).mock.instances).toContain(target));
  });

  it("puts an unknown tab back on Connections", async () => {
    renderAt("/settings/bogus");
    expect(await screen.findByRole("tab", { name: "Connections", selected: true })).toBeVisible();
    await waitFor(() => expect(address()).toBe("/settings/connections"));
  });

  it("keeps the webhook switch reachable from #notifications, beside its connection", async () => {
    renderAt("/settings#notifications");
    expect(await screen.findByRole("switch", { name: "Send alerts to a webhook" })).toBeVisible();
    expect(within(document.getElementById("notifications")!).getByTestId("connection-notify")).toBeVisible();
  });

  it("keeps the uninstall link on System", async () => {
    renderAt("/settings#danger");
    expect(await screen.findByRole("link", { name: "Uninstall Shortlist…" })).toHaveAttribute("href", "/settings/uninstall");
  });
});

describe("the tab strip", () => {
  it.each(["connections", "defaults", "system"])("offers AI assistants from the %s header", async (tab) => {
    renderAt(`/settings/${tab}`);
    const link = screen.getByRole("link", { name: "AI assistants" });
    expect(link).toHaveAttribute("href", "/assistant-access");
    await userEvent.click(link);
    expect(address()).toBe("/assistant-access");
  });
  it("switches tabs by address, and keeps an unsaved draft on the tab it left", async () => {
    renderAt("/settings/defaults");
    const name = await screen.findByLabelText("Row name template");
    await userEvent.clear(name);
    await userEvent.type(name, "Tonight");
    await userEvent.click(screen.getByRole("tab", { name: "System" }));
    await waitFor(() => expect(address()).toBe("/settings/system"));
    expect(screen.getByLabelText("Row name template")).not.toBeVisible();
    await userEvent.click(screen.getByRole("tab", { name: "Defaults" }));
    expect(screen.getByLabelText("Row name template")).toHaveValue("Tonight");
  });

  it("lists the Defaults sections in a jump list", async () => {
    renderAt("/settings/defaults");
    const jumps = await screen.findByRole("navigation", { name: "Defaults sections" });
    const links = within(jumps).getAllByRole("link");
    expect(links.map((link) => link.textContent)).toEqual([
      "Title sources",
      "Refresh & variety",
      "Row defaults",
      "Row placement",
    ]);
    expect(links.map((link) => link.getAttribute("href"))).toEqual([
      "/settings/defaults#sources",
      "/settings/defaults#refresh",
      "/settings/defaults#row-defaults",
      "/settings/defaults#placement",
    ]);
  });
});

describe("one Webhook, and the switch that moved", () => {
  it("shows the Webhook exactly once across the three tabs", async () => {
    renderAt("/settings/connections");
    await screen.findByRole("tab", { name: "Connections", selected: true });
    let seen = 0;
    for (const tab of ["Connections", "Defaults", "System"]) {
      await userEvent.click(screen.getByRole("tab", { name: tab }));
      seen += screen.queryAllByText("Webhook", { exact: true }).filter(visible).length;
    }
    expect(seen).toBe(1);
  });

  it("no longer offers 'Disabled users see nothing' on any tab — it lives on Privacy", async () => {
    renderAt("/settings/system");
    await screen.findByRole("tab", { name: "System", selected: true });
    expect(screen.queryByRole("switch", { name: /disabled users/i, hidden: true })).toBeNull();
    expect(screen.queryByText("Disabled users see nothing")).toBeNull();
  });
});

describe("Search settings", () => {
  it("opens AI assistants from an MCP search", async () => {
    renderAt("/settings/connections");
    await userEvent.type(screen.getByRole("combobox", { name: /search settings/i }), "MCP");
    await userEvent.click(screen.getByRole("option", { name: /AI assistants/ }));
    expect(address()).toBe("/assistant-access");
  });
  it("finds a setting on another tab and jumps to it", async () => {
    renderAt("/settings/connections");
    const search = await screen.findByRole("combobox", { name: /search settings/i });
    await userEvent.type(search, "concurrency");
    await userEvent.click(screen.getByRole("option", { name: /Run concurrency/ }));
    await waitFor(() => expect(address()).toBe("/settings/system#run-concurrency"));
    expect(screen.getByRole("tab", { name: "System", selected: true })).toBeVisible();
    expect(search).toHaveValue("");
  });

  it("says 'Disabled users see nothing' moved to Privacy, and goes there", async () => {
    renderAt("/settings/connections");
    const search = await screen.findByRole("combobox", { name: /search settings/i });
    await userEvent.type(search, "disabled users");
    const option = screen.getByRole("option", { name: /Disabled users see nothing/ });
    expect(option).toHaveTextContent("Moved to Privacy");
    await userEvent.keyboard("{Enter}");
    await waitFor(() => expect(address()).toBe("/privacy"));
  });

  it("is focused by the / key, and Escape clears it", async () => {
    renderAt("/settings/connections");
    const search = await screen.findByRole("combobox", { name: /search settings/i });
    fireEvent.keyDown(document.body, { key: "/" });
    expect(search).toHaveFocus();
    await userEvent.type(search, "plex");
    expect(screen.getAllByRole("option").length).toBeGreaterThan(0);
    await userEvent.keyboard("{Escape}");
    expect(search).toHaveValue("");
    expect(screen.queryByRole("option")).toBeNull();
  });

  it("says when nothing matches", async () => {
    renderAt("/settings/connections");
    const search = await screen.findByRole("combobox", { name: /search settings/i });
    await userEvent.type(search, "zzzz");
    expect(screen.getByText(/No setting matches/)).toBeVisible();
  });
});
