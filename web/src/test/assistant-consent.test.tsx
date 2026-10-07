import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type * as QueriesModule from "@/lib/queries";
import type { AssistantConsentFlow, AssistantGrant, AssistantGrantConstraints, AssistantStatus } from "@/lib/types";
import { AssistantConsentPage } from "@/pages/assistant-consent";

const { beginAssistantConsent, getAssistantStatus, getAssistantGrants, getAssistantDestinations, createAssistantGrant, decideAssistantConsent } = vi.hoisted(() => ({
  beginAssistantConsent: vi.fn(), getAssistantStatus: vi.fn(), getAssistantGrants: vi.fn(),
  getAssistantDestinations: vi.fn(), createAssistantGrant: vi.fn(), decideAssistantConsent: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, beginAssistantConsent, getAssistantStatus, getAssistantGrants, getAssistantDestinations, createAssistantGrant, decideAssistantConsent } };
});
vi.mock("@/lib/queries", async (importOriginal) => {
  const actual = await importOriginal<typeof QueriesModule>();
  return { ...actual, useSession: () => ({ data: { authenticated: true }, isPending: false, isError: false }) };
});

const status: AssistantStatus = {
  enabled: true, resource: "https://shortlist.example/mcp", issuer: "https://shortlist.example/assistant/oauth",
  configuration_error: null, configuration_hint: "",
  presets: { inspect: ["instance.read"], manage_selected_rows: ["instance.read", "rows.update"], owner_automation: ["instance.read", "rows.update", "config.write"] },
  setting_groups: ["schedule", "row_defaults"],
};

function flow(requested_scopes: string[]): AssistantConsentFlow {
  return { flow_id: "flow", csrf_token: "csrf", client: { id: "client", name: "Claude" }, requested_scopes, resource: "https://shortlist.example/mcp", expires_at: "2026-10-10T00:00:00Z" };
}

function emptyConstraintsForTest(): AssistantGrantConstraints {
  return {
    row_ids: [], library_keys: [], setting_groups: [], destination_ids: [],
    include_future_rows: true, include_future_libraries: true,
    max_batch_size: 25, max_work_per_operation: null, max_provider_calls: 0,
  };
}

function renderConsent() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><AssistantConsentPage /></QueryClientProvider>);
}

describe("assistant OAuth consent mapping", () => {
  beforeEach(() => {
    for (const mock of [beginAssistantConsent, getAssistantStatus, getAssistantGrants, getAssistantDestinations, createAssistantGrant, decideAssistantConsent]) mock.mockReset();
    getAssistantStatus.mockResolvedValue(status);
    getAssistantGrants.mockResolvedValue([]);
    getAssistantDestinations.mockResolvedValue([]);
    createAssistantGrant.mockResolvedValue({ id: "new-grant" });
    decideAssistantConsent.mockReturnValue(new Promise(() => {}));
  });

  it("defaults management requests to Manage but grants only requested baseline rights", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read", "rows.update", "ai.generate", "history.export"]));
    renderConsent();

    expect(await screen.findByRole("button", { name: "Manage Shortlist" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Allow this assistant to use paid services")).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      preset: "owner_automation", capabilities: ["instance.read", "rows.update"],
      constraints: expect.objectContaining({ setting_groups: ["schedule", "row_defaults"], max_provider_calls: 0 }),
    })));
  });

  it("starts a read-only request in Suggest mode with no management groups", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read"]));
    renderConsent();

    expect(await screen.findByRole("button", { name: "Suggest changes" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByRole("button", { name: "Manage Shortlist" })).toBeDisabled();
    expect(screen.getByLabelText("Allow this assistant to use paid services")).toBeDisabled();
    expect(screen.getByLabelText("Show viewing details to this assistant")).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      preset: "inspect", capabilities: ["instance.read"],
      constraints: expect.objectContaining({ setting_groups: [] }),
    })));
  });

  it("validates an enabled paid allowance before approving OAuth consent", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read", "rows.update", "ai.generate"]));
    renderConsent();

    await user.click(await screen.findByLabelText("Allow this assistant to use paid services"));
    const allowance = screen.getByLabelText("Lifetime call allowance");
    await user.clear(allowance);
    expect(allowance).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
    await user.type(allowance, "101");
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
    await user.clear(allowance);
    await user.type(allowance, "2");
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      capabilities: ["instance.read", "rows.update", "ai.generate"],
      constraints: expect.objectContaining({ max_provider_calls: 2 }),
    })));
  });

  it("uses an existing restricted grant without replacing its capability or resource set", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read", "rows.update"]));
    getAssistantGrants.mockResolvedValue([{ id: "existing", client_id: "client", name: "Restricted Claude", capabilities: ["instance.read", "rows.update"], requires_access_approval: false, revoked_at: null, expires_at: null, constraints: { ...emptyConstraintsForTest(), row_ids: [17], library_keys: ["2"], include_future_rows: false, include_future_libraries: false } } as AssistantGrant]);
    renderConsent();

    expect(await screen.findByText(/1 selected rows; 1 selected libraries/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(decideAssistantConsent).toHaveBeenCalledWith({ flow_id: "flow", csrf_token: "csrf", approved: true, grant_id: "existing" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
  });
});
