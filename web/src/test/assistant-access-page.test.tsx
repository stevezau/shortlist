import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type { AssistantGrant, AssistantStatus } from "@/lib/types";
import { AssistantAccessPage } from "@/pages/assistant-access";

const { getAssistantStatus, getAssistantGrants, getAssistantDestinations, createAssistantGrant, updateAssistantGrant } = vi.hoisted(() => ({
  getAssistantStatus: vi.fn(), getAssistantGrants: vi.fn(), getAssistantDestinations: vi.fn(),
  createAssistantGrant: vi.fn(), updateAssistantGrant: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, getAssistantStatus, getAssistantGrants, getAssistantDestinations, createAssistantGrant, updateAssistantGrant } };
});

const status: AssistantStatus = {
  enabled: true, resource: "https://shortlist.example/mcp", issuer: "https://shortlist.example/assistant/oauth",
  configuration_error: null, configuration_hint: "", presets: { inspect: [], manage_selected_rows: [], owner_automation: [] }, setting_groups: [],
};
function grant(overrides: Partial<AssistantGrant> = {}): AssistantGrant {
  return {
    id: "old-grant", owner_account_id: 1, client_id: "old-client", name: "Earlier assistant", preset: "inspect",
    access_role: null, capabilities: ["instance.read", "ai.generate"],
    constraints: {
      owner_managed: false, row_ids: [17], library_keys: ["2"], setting_groups: ["schedule"],
      destination_ids: ["https://historical.example"], include_future_rows: false, include_future_libraries: false,
      max_batch_size: 25, max_work_per_operation: 100, max_provider_calls: 0,
    },
    revision: 4, requires_access_approval: false, expires_at: "2027-01-01T00:00:00Z",
    full_management: false, provider_call_quota: { lifetime_limit: 0, reserved: 1, remaining: 0 },
    ...overrides,
  };
}
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><AssistantAccessPage /></MemoryRouter></QueryClientProvider>);
}

describe("simple assistant owner access", () => {
  beforeEach(() => {
    for (const mock of [getAssistantStatus, getAssistantGrants, getAssistantDestinations, createAssistantGrant, updateAssistantGrant]) mock.mockReset();
    getAssistantStatus.mockResolvedValue(status);
    getAssistantGrants.mockResolvedValue([grant()]);
    getAssistantDestinations.mockResolvedValue(Array.from({ length: 9 }, (_, i) => ({ service_id: `service-${i}`, label: `Service ${i}`, destination_id: `https://service-${i}.example` })));
    createAssistantGrant.mockResolvedValue(grant());
    updateAssistantGrant.mockResolvedValue(grant());
  });

  it("creates Manage by default with only a name and two roles even with nine services configured", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New connection" }));
    expect(screen.getByRole("radio", { name: /Manage Shortlist/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /View only/ })).not.toBeChecked();
    expect(screen.getByText(/Runs may incur provider charges/)).toBeVisible();
    expect(screen.queryByText("Service 0")).not.toBeInTheDocument();
    expect(screen.queryByText(/Advanced|Setting groups|Lifetime call allowance/)).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Connection name"), "Desk assistant");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith({
      client_id: expect.stringMatching(/^local-/), name: "Desk assistant", preset: "owner_automation", access_role: "manage",
    }));
    expect(getAssistantDestinations).not.toHaveBeenCalled();
  });

  it("creates View only when deliberately selected and sends no paid or resource selections", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.type(screen.getByLabelText("Connection name"), "Reader");
    await user.click(screen.getByRole("radio", { name: /View only/ }));
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith({
      client_id: expect.stringMatching(/^local-/), name: "Reader", preset: "owner_automation", access_role: "view",
    }));
  });

  it("keeps a restricted legacy connection unchanged until explicit revision-guarded role save", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(/^Existing access/)).toBeVisible();
    expect(updateAssistantGrant).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Change access" }));
    expect(screen.getByRole("button", { name: "Save access" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(updateAssistantGrant).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Change access" }));
    await user.click(screen.getByRole("radio", { name: /Manage Shortlist/ }));
    await user.click(screen.getByRole("button", { name: "Save access" }));
    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("old-grant", { expected_revision: 4, access_role: "manage" }));
  });

  it("truthfully labels OAuth-limited Manage and allows deliberate same-role restoration", async () => {
    const user = userEvent.setup();
    getAssistantGrants.mockResolvedValue([grant({ access_role: "manage", capabilities: ["instance.read"], full_management: false })]);
    renderPage();
    expect(await screen.findByText(/Manage Shortlist · limited by client permissions/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Change access" }));
    expect(screen.getByRole("button", { name: "Save access" })).toBeEnabled();
    await user.click(screen.getByRole("button", { name: "Save access" }));
    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("old-grant", { expected_revision: 4, access_role: "manage" }));
  });
});
