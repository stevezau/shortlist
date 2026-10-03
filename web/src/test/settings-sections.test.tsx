import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import type { ReactNode } from "react";
import { Link, MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SettingDisclosure } from "@/components/settings/setting-disclosure";
import { SettingsTabs } from "@/components/settings/section-layout";

function HistoryButtons() {
  const navigate = useNavigate();
  return (
    <>
      <button onClick={() => void navigate(-1)}>Back</button>
      <button onClick={() => void navigate(1)}>Forward</button>
    </>
  );
}

function Where() {
  const { pathname, search, hash } = useLocation();
  return <output aria-label="Address">{`${pathname}${search}${hash}`}</output>;
}

function renderTabs(path: string, content: Parameters<typeof SettingsTabs>[0]["content"], extra?: ReactNode) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      {extra}
      <Routes>
        <Route path="/settings/:tab?" element={<SettingsTabs content={content} />} />
      </Routes>
      <Where />
    </MemoryRouter>,
  );
}

const scrollIntoView = vi.fn();
const lastScrolled = () => scrollIntoView.mock.instances.at(-1);

describe("Settings tabs", () => {
  beforeEach(() => {
    scrollIntoView.mockClear();
    Element.prototype.scrollIntoView = scrollIntoView;
  });

  it("keeps every tab mounted, so a draft survives a trip to another tab and back through history", async () => {
    renderTabs(
      "/settings/connections",
      { connections: <input aria-label="Draft" defaultValue="" />, defaults: <p>Defaults body</p>, system: <p>System body</p> },
      <HistoryButtons />,
    );
    await userEvent.type(screen.getByLabelText("Draft"), "keep this");
    await userEvent.click(screen.getByRole("tab", { name: "Defaults" }));
    expect(screen.getByText("Defaults body")).toBeVisible();
    expect(screen.getByLabelText("Draft")).not.toBeVisible();
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    await waitFor(() => expect(screen.getByLabelText("Draft")).toBeVisible());
    await userEvent.click(screen.getByRole("button", { name: "Forward" }));
    await waitFor(() => expect(screen.getByText("Defaults body")).toBeVisible());
    await userEvent.click(screen.getByRole("tab", { name: "Connections" }));
    expect(screen.getByLabelText("Draft")).toHaveValue("keep this");
  });

  it("repeats a jump when its hash is already in the address", async () => {
    renderTabs("/settings/defaults#requests", {
      connections: null,
      defaults: (
        <>
          <Link to="/settings/defaults#requests">Requests</Link>
          <section id="requests" />
        </>
      ),
      system: null,
    });
    await waitFor(() => expect(lastScrolled()).toBe(document.getElementById("requests")));
    scrollIntoView.mockClear();
    await userEvent.click(screen.getByRole("link", { name: "Requests" }));
    await waitFor(() => expect(lastScrolled()).toBe(document.getElementById("requests")));
  });

  it("switches to the tab an in-page anchor belongs to", async () => {
    renderTabs("/settings/defaults?view=x", {
      connections: <section id="connections">Connection controls</section>,
      defaults: <Link to="/settings/defaults?view=x#connections">change it in Connections</Link>,
      system: null,
    });
    await userEvent.click(screen.getByRole("link", { name: "change it in Connections" }));
    await waitFor(() => expect(screen.getByLabelText("Address")).toHaveTextContent("/settings/connections?view=x#connections"));
    expect(screen.getByText("Connection controls")).toBeVisible();
    await waitFor(() => expect(lastScrolled()).toBe(document.getElementById("connections")));
  });

  it("finds a field it doesn't list by looking in each tab", async () => {
    renderTabs("/settings/connections#custom-field", {
      connections: null,
      defaults: null,
      system: <input id="custom-field" aria-label="Custom field" />,
    });
    await waitFor(() => expect(screen.getByRole("tab", { name: "System", selected: true })).toBeVisible());
    expect(screen.getByLabelText("Custom field")).toBeVisible();
  });

  it("opens secondary controls before scrolling to a deep-linked field", async () => {
    renderTabs("/settings#min-history", {
      connections: <p>Connection controls</p>,
      defaults: (
        <SettingDisclosure title="More controls" value="Edit">
          <input id="min-history" aria-label="Deep linked history" />
        </SettingDisclosure>
      ),
      system: null,
    });
    await waitFor(() => expect(screen.getByLabelText("Address")).toHaveTextContent("/settings/defaults#min-history"));
    expect(screen.getByLabelText("Deep linked history")).toBeVisible();
    await waitFor(() => expect(lastScrolled()).toBe(document.getElementById("min-history")));
  });
});
