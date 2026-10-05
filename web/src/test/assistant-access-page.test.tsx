import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { AssistantAccessPage } from "@/pages/assistant-access";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getAssistantStatus: () => Promise.resolve({
        enabled: false,
        configuration_error: "SHORTLIST_MCP_URL is not configured.",
        configuration_hint: "Set the canonical MCP URL, then restart Shortlist.",
        presets: {},
        setting_groups: [],
      }),
      getUsers: () => Promise.resolve([]),
      listCollections: () => Promise.resolve([]),
      getLibraries: () => Promise.resolve([]),
    },
  };
});

describe("AI assistants page", () => {
  it("explains the benefit and preserves setup instructions when disabled", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(<QueryClientProvider client={client}><MemoryRouter><AssistantAccessPage /></MemoryRouter></QueryClientProvider>);

    expect(await screen.findByRole("heading", { name: "AI assistants", level: 1 })).toBeVisible();
    expect(screen.getByText(/Connect ChatGPT, Claude or Codex to set up and manage Shortlist/)).toBeVisible();
    expect(screen.getByText(/You choose each connection’s permissions and limits/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "AI assistants are off" })).toBeVisible();
    expect(screen.getByText("SHORTLIST_MCP_URL is not configured.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "New connection" })).not.toBeInTheDocument();
  });
});
