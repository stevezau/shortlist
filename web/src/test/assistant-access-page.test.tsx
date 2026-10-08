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
    capabilities: ["instance.read", "ai.generate"],
    constraints: {
      owner_managed: false, row_ids: [17], library_keys: ["2"], setting_groups: ["schedule"],
      destination_ids: ["https://historical.example"], include_future_rows: false, include_future_libraries: false,
      max_batch_size: 25, max_work_per_operation: 100, max_provider_calls: 0,
    },
    revision: 4, requires_access_approval: true, expires_at: "2027-01-01T00:00:00Z",
    full_management: false,
    provider_call_quota: { lifetime_limit: 0, reserved: 1, remaining: 0 },
    ...overrides,
  };
}
function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><AssistantAccessPage /></MemoryRouter></QueryClientProvider>);
}

describe("simple assistant owner consent", () => {
  beforeEach(() => {
    for (const mock of [getAssistantStatus, getAssistantGrants, getAssistantDestinations, createAssistantGrant, updateAssistantGrant]) mock.mockReset();
    getAssistantStatus.mockResolvedValue(status);
    getAssistantGrants.mockResolvedValue([grant()]);
    getAssistantDestinations.mockResolvedValue(Array.from({ length: 9 }, (_, i) => ({ service_id: `service-${i}`, label: `Service ${i}`, destination_id: `https://service-${i}.example` })));
    createAssistantGrant.mockResolvedValue(grant());
    updateAssistantGrant.mockResolvedValue(grant());
  });

  it("keeps nine configured services out of the short new-connection form", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New connection" }));
    expect(screen.getByText(/everyone’s rows and viewing details/)).toBeVisible();
    expect(screen.getByText(/services you configure now or later/)).toBeVisible();
    expect(screen.queryByText("Service 0")).not.toBeInTheDocument();
    expect(screen.queryByText(/Setting groups/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Advanced/)).not.toBeInTheDocument();
    expect(screen.queryByRole("radio")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("Connection name"), "Desk assistant");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith({
      client_id: expect.stringMatching(/^local-/), name: "Desk assistant", preset: "owner_automation",
      owner_managed: true, constraints: { max_provider_calls: 0 },
    }));
    expect(getAssistantDestinations).not.toHaveBeenCalled();
  });

  it("keeps a cleared or out-of-range paid allowance visible and blocks submission", async () => {
    const user = userEvent.setup();
    renderPage();
    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.type(screen.getByLabelText("Connection name"), "Paid assistant");
    await user.click(screen.getByLabelText("Allow this assistant to use paid services"));
    const amount = screen.getByLabelText("Lifetime call allowance");
    await user.clear(amount);
    expect(amount).toBeInvalid();
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
    await user.type(amount, "101");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
    await user.clear(amount);
    await user.type(amount, "2");
    await user.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({ constraints: { max_provider_calls: 2 } })));
  });

  it("leaves legacy access untouched on render and cancel, then upgrades by revision without editing paid state", async () => {
    const user = userEvent.setup();
    renderPage();
    expect(await screen.findByText(/^Existing limited access/)).toBeVisible();
    expect(updateAssistantGrant).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Upgrade to full Shortlist access" }));
    expect(screen.getByText(/Its expiry and paid usage remain unchanged/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    expect(updateAssistantGrant).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Upgrade to full Shortlist access" }));
    await user.click(screen.getAllByRole("button", { name: "Upgrade to full Shortlist access" }).at(-1)!);
    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("old-grant", {
      expected_revision: 4, upgrade_owner_managed: true,
    }));
  });

  it("edits only paid access on an existing full connection and accounts for reserved calls", async () => {
    const user = userEvent.setup();
    getAssistantGrants.mockResolvedValue([grant({
      constraints: { ...grant().constraints, owner_managed: true }, full_management: true, requires_access_approval: false,
    })]);
    renderPage();
    await user.click(await screen.findByRole("button", { name: "Paid access" }));
    await user.click(screen.getByLabelText("Allow this assistant to use paid services"));
    expect(screen.getByLabelText("Lifetime call allowance")).toHaveValue(2);
    await user.click(screen.getByRole("button", { name: "Save paid access" }));
    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("old-grant", {
      expected_revision: 4, paid_enabled: true, max_provider_calls: 2,
    }));
  });

  it("offers explicit upgrade for a profile-marked but OAuth-limited connection", async () => {
    const user = userEvent.setup();
    getAssistantGrants.mockResolvedValue([grant({
      constraints: { ...grant().constraints, owner_managed: true },
      capabilities: ["instance.read"], full_management: false, requires_access_approval: false,
    })]);
    renderPage();
    expect(await screen.findByText(/^Existing limited access/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Upgrade to full Shortlist access" }));
    await user.click(screen.getAllByRole("button", { name: "Upgrade to full Shortlist access" }).at(-1)!);
    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("old-grant", {
      expected_revision: 4, upgrade_owner_managed: true,
    }));
  });

  it("does not advertise stored allowance as spendable without paid permission", async () => {
    getAssistantGrants.mockResolvedValue([grant({
      capabilities: ["instance.read"],
      constraints: { ...grant().constraints, max_provider_calls: 5 },
      provider_call_quota: { lifetime_limit: 5, reserved: 1, remaining: 4 },
    })]);
    renderPage();
    expect(await screen.findByText(/0 paid calls available/)).toBeVisible();
    expect(screen.queryByText(/4 paid calls available/)).not.toBeInTheDocument();
  });
});
