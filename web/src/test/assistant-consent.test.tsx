import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import type * as QueriesModule from "@/lib/queries";
import type { AssistantConsentFlow, AssistantGrant, AssistantStatus } from "@/lib/types";
import { AssistantConsentPage } from "@/pages/assistant-consent";

const { beginAssistantConsent, getAssistantStatus, getAssistantGrants, createAssistantGrant, decideAssistantConsent } = vi.hoisted(() => ({
  beginAssistantConsent: vi.fn(), getAssistantStatus: vi.fn(), getAssistantGrants: vi.fn(),
  createAssistantGrant: vi.fn(), decideAssistantConsent: vi.fn(),
}));
vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, beginAssistantConsent, getAssistantStatus, getAssistantGrants, createAssistantGrant, decideAssistantConsent } };
});
vi.mock("@/lib/queries", async (importOriginal) => {
  const actual = await importOriginal<typeof QueriesModule>();
  return { ...actual, useSession: () => ({ data: { authenticated: true }, isPending: false, isError: false }) };
});

const status: AssistantStatus = {
  enabled: true, resource: "https://shortlist.example/mcp", issuer: "https://shortlist.example/assistant/oauth",
  configuration_error: null, configuration_hint: "",
  presets: { inspect: ["instance.read"], manage_selected_rows: ["instance.read", "rows.update"], owner_automation: ["instance.read", "rows.update"] },
  setting_groups: [],
};
function flow(requested_scopes: string[]): AssistantConsentFlow {
  return { flow_id: "flow", csrf_token: "csrf", client: { id: "client", name: "Claude" }, requested_scopes, resource: "https://shortlist.example/mcp", expires_at: "2026-10-10T00:00:00Z" };
}
function renderConsent() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><AssistantConsentPage /></QueryClientProvider>);
}

describe("two-role OAuth owner consent", () => {
  beforeEach(() => {
    for (const mock of [beginAssistantConsent, getAssistantStatus, getAssistantGrants, createAssistantGrant, decideAssistantConsent]) mock.mockReset();
    getAssistantStatus.mockResolvedValue(status);
    getAssistantGrants.mockResolvedValue([]);
    createAssistantGrant.mockResolvedValue({ id: "new-grant" });
    decideAssistantConsent.mockReturnValue(new Promise(() => {}));
  });

  it("creates a Manage grant intersected with requested scopes and no paid or resource inputs", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read", "rows.update", "history.export", "ai.generate"]));
    renderConsent();
    expect(await screen.findByRole("radio", { name: /Manage Shortlist/ })).toBeChecked();
    expect(screen.getByText(/limited permissions/)).toBeVisible();
    expect(screen.queryByText(/Services this assistant can use|Lifetime call allowance|Advanced/)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith({
      client_id: "client", name: "Claude", preset: "owner_automation", access_role: "manage",
      capabilities: ["instance.read", "rows.update", "history.export", "ai.generate"],
    }));
  });

  it("defaults a read-only client to View and disables an unusable Manage choice", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read"]));
    renderConsent();
    expect(await screen.findByRole("radio", { name: /View only/ })).toBeChecked();
    expect(screen.getByRole("radio", { name: /Manage Shortlist/ })).toBeDisabled();
    expect(screen.getByText(/requested read-only access/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      access_role: "view", capabilities: ["instance.read"],
    })));
  });

  it("lets a wider client choose View without widening the approved scope", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read", "rows.update"]));
    renderConsent();
    await user.click(await screen.findByRole("radio", { name: /View only/ }));
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      access_role: "view", capabilities: ["instance.read", "rows.update"],
    })));
    expect(decideAssistantConsent).toHaveBeenCalledWith({ flow_id: "flow", csrf_token: "csrf", approved: true, grant_id: "new-grant" });
  });

  it("reuses a limited existing grant without upgrading or changing historical quota", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read"]));
    getAssistantGrants.mockResolvedValue([{
      id: "existing", client_id: "client", name: "Restricted Claude", access_role: null, capabilities: ["instance.read"],
      full_management: false, requires_access_approval: false, revoked_at: null, expires_at: null,
      constraints: { owner_managed: false, max_provider_calls: 5 },
    } as AssistantGrant]);
    renderConsent();
    expect(await screen.findByText(/This sign-in is read-only/)).toBeVisible();
    expect(screen.getByText(/saved connection keeps its existing access/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(decideAssistantConsent).toHaveBeenCalledWith({ flow_id: "flow", csrf_token: "csrf", approved: true, grant_id: "existing" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
  });
});
