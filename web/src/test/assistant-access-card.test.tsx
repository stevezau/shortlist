import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AssistantAccessCard } from "@/components/settings/assistant-access-card";
import type * as ApiModule from "@/lib/api";
import type { AssistantStatus } from "@/lib/types";

const { getAssistantStatus } = vi.hoisted(() => ({
  getAssistantStatus: vi.fn<() => Promise<AssistantStatus>>(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: { ...actual.api, getAssistantStatus },
  };
});

function renderCard() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <AssistantAccessCard />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

function status(enabled: boolean): AssistantStatus {
  return {
    enabled,
    resource: enabled ? "https://media.example/shortlist/mcp" : null,
    issuer: enabled ? "https://media.example/shortlist/assistant/oauth" : null,
    configuration_error: enabled ? null : "SHORTLIST_MCP_URL is not configured.",
    configuration_hint: "Set the canonical MCP URL, then restart Shortlist.",
    presets: {
      inspect: ["instance.read"],
      manage_selected_rows: ["instance.read", "rows.update"],
      owner_automation: ["instance.read", "runs.execute"],
    },
    setting_groups: [],
  };
}

describe("AssistantAccessCard", () => {
  beforeEach(() => { getAssistantStatus.mockReset(); });

  it("shows enabled named access and links to grant management", async () => {
    getAssistantStatus.mockResolvedValue(status(true));
    renderCard();

    expect(await screen.findByText("Enabled")).toBeVisible();
    expect(screen.getByRole("heading", { name: "AI assistants" })).toBeVisible();
    expect(screen.getByRole("link", { name: /manage connections/i })).toHaveAttribute(
      "href",
      "/assistant-access",
    );
    expect(screen.getByText(/without sharing your owner API token/i)).toBeVisible();
    expect(screen.getByText(/set up and manage Shortlist/i)).toBeVisible();
  });

  it("explains how to enable the endpoint without offering a credential", async () => {
    getAssistantStatus.mockResolvedValue(status(false));
    renderCard();

    expect(await screen.findByText("Off")).toBeVisible();
    expect(screen.getByText(/SHORTLIST_MCP_URL is not configured/i)).toBeVisible();
    expect(screen.getByRole("link", { name: /connect an assistant/i })).toHaveAttribute("href", "/assistant-access");
    expect(screen.queryByText(/shla_/i)).not.toBeInTheDocument();
  });

  it("does not invent a status while loading", () => {
    getAssistantStatus.mockReturnValue(new Promise(() => {}));
    renderCard();
    expect(screen.queryByText("Off")).not.toBeInTheDocument();
    expect(screen.queryByText("Enabled")).not.toBeInTheDocument();
  });

  it("lets the owner retry a status error", async () => {
    getAssistantStatus.mockRejectedValueOnce(new Error("Unavailable"));
    renderCard();
    expect(await screen.findByRole("alert")).toHaveTextContent("Couldn’t check assistant access");
    expect(screen.queryByText("Off")).not.toBeInTheDocument();
    getAssistantStatus.mockResolvedValue(status(true));
    await userEvent.click(screen.getByRole("button", { name: "Try again" }));
    expect(await screen.findByText("Enabled")).toBeVisible();
  });
});
