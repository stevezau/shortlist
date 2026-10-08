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

describe("simple OAuth owner consent", () => {
  beforeEach(() => {
    for (const mock of [beginAssistantConsent, getAssistantStatus, getAssistantGrants, createAssistantGrant, decideAssistantConsent]) mock.mockReset();
    getAssistantStatus.mockResolvedValue(status);
    getAssistantGrants.mockResolvedValue([]);
    createAssistantGrant.mockResolvedValue({ id: "new-grant" });
    decideAssistantConsent.mockReturnValue(new Promise(() => {}));
  });

  it("creates a scoped owner-managed grant without mode, service, or resource choices", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read", "rows.update", "history.export", "ai.generate"]));
    renderConsent();
    expect(await screen.findByText(/permissions it requested/)).toBeVisible();
    expect(screen.queryByRole("radio", { name: /Manage Shortlist|Suggest changes/ })).not.toBeInTheDocument();
    expect(screen.queryByText(/Services this assistant can use/)).not.toBeInTheDocument();
    expect(screen.getByLabelText("Allow this assistant to use paid services")).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith({
      client_id: "client", name: "Claude", preset: "owner_automation", owner_managed: true,
      capabilities: ["instance.read", "rows.update", "history.export", "ai.generate"],
      constraints: { max_provider_calls: 0 },
    }));
  });

  it("tells a read-only client its requested permissions are the ceiling and hides paid usage", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read"]));
    renderConsent();
    expect(await screen.findByText(/requested read-only access to Shortlist/)).toBeVisible();
    expect(screen.getByText(/It cannot change your setup/)).toBeVisible();
    expect(screen.queryByText(/may include managing rows/)).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Allow this assistant to use paid services")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      capabilities: ["instance.read"], constraints: { max_provider_calls: 0 },
    })));
  });

  it("shows the full-management disclosure when the client requested that complete profile", async () => {
    beginAssistantConsent.mockResolvedValue(flow([
      "instance.read", "rows.update", "history.export", "history.providers", "requests.send", "maintenance.execute",
    ]));
    renderConsent();
    expect(await screen.findByText(/everyone’s rows and viewing details/)).toBeVisible();
    expect(screen.queryByText(/cannot gain more through this approval/)).not.toBeInTheDocument();
  });

  it("uses native validation for paid allowance and keeps Deny available", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read", "ai.generate"]));
    renderConsent();
    await user.click(await screen.findByLabelText("Allow this assistant to use paid services"));
    const amount = screen.getByLabelText("Lifetime call allowance");
    await user.clear(amount);
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    expect(amount).toBeVisible();
    expect(amount).toBeInvalid();
    expect(createAssistantGrant).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Deny" }));
    await waitFor(() => expect(decideAssistantConsent).toHaveBeenCalledWith({ flow_id: "flow", csrf_token: "csrf", approved: false, grant_id: null }));
  });

  it("reuses a limited existing grant without widening or changing its quota", async () => {
    const user = userEvent.setup();
    beginAssistantConsent.mockResolvedValue(flow(["instance.read"]));
    getAssistantGrants.mockResolvedValue([{
      id: "existing", client_id: "client", name: "Restricted Claude", capabilities: ["instance.read"],
      full_management: false,
      requires_access_approval: false, revoked_at: null, expires_at: null,
      constraints: { owner_managed: false, max_provider_calls: 0 },
    } as AssistantGrant]);
    renderConsent();
    expect(await screen.findByText(/This sign-in is read-only/)).toBeVisible();
    expect(screen.getByText(/Your saved connection keeps its existing access/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Allow connection" }));
    await waitFor(() => expect(decideAssistantConsent).toHaveBeenCalledWith({ flow_id: "flow", csrf_token: "csrf", approved: true, grant_id: "existing" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
  });
});
