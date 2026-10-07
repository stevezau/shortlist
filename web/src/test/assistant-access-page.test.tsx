import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "@/lib/api";
import type * as ApiModule from "@/lib/api";
import type { AssistantGrant, AssistantStatus } from "@/lib/types";
import { AssistantAccessPage } from "@/pages/assistant-access";

const {
  getAssistantStatus,
  getAssistantGrants,
  getAssistantDestinations,
  getUsers,
  listCollections,
  getLibraries,
  createAssistantGrant,
  updateAssistantGrant,
  removeAssistantGrant,
} = vi.hoisted(() => ({
  getAssistantStatus: vi.fn(),
  getAssistantGrants: vi.fn(),
  getAssistantDestinations: vi.fn(),
  getUsers: vi.fn(),
  listCollections: vi.fn(),
  getLibraries: vi.fn(),
  createAssistantGrant: vi.fn(),
  updateAssistantGrant: vi.fn(),
  removeAssistantGrant: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getAssistantStatus,
      getAssistantGrants,
      getAssistantDestinations,
      getUsers,
      listCollections,
      getLibraries,
      createAssistantGrant,
      updateAssistantGrant,
      removeAssistantGrant,
    },
  };
});

const activeStatus: AssistantStatus = {
  enabled: true,
  resource: "https://shortlist.example/assistant",
  issuer: "https://shortlist.example/assistant/oauth",
  configuration_error: null,
  configuration_hint: "",
  presets: {
    inspect: ["instance.read"],
    manage_selected_rows: ["instance.read", "rows.update"],
    owner_automation: ["instance.read", "rows.update", "runs.execute"],
  },
  setting_groups: ["schedule"],
};

function activeGrant(overrides: Partial<AssistantGrant> = {}): AssistantGrant {
  return {
    id: "grant-codex",
    owner_account_id: 1,
    client_id: "codex",
    name: "Seasonal Codex",
    preset: "inspect",
    capabilities: ["instance.read", "rows.update"],
    constraints: {
      row_ids: [17],
      library_keys: ["2"],
      setting_groups: ["schedule"],
      destination_ids: ["https://search.example"],
      include_future_rows: false,
      include_future_libraries: false,
      max_batch_size: 25,
      max_work_per_operation: 100,
      max_provider_calls: 3,
    },
    revision: 4,
    requires_access_approval: false,
    expires_at: null,
    local_credential_count: 1,
    ...overrides,
  };
}

function renderPage() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(<QueryClientProvider client={client}><MemoryRouter><AssistantAccessPage /></MemoryRouter></QueryClientProvider>);
  return client;
}

describe("AI assistants page", () => {
  afterEach(() => vi.useRealTimers());

  beforeEach(() => {
    getAssistantStatus.mockReset();
    getAssistantGrants.mockReset();
    getAssistantDestinations.mockReset();
    getUsers.mockReset();
    listCollections.mockReset();
    getLibraries.mockReset();
    createAssistantGrant.mockReset();
    updateAssistantGrant.mockReset();
    removeAssistantGrant.mockReset();
    getAssistantStatus.mockResolvedValue(activeStatus);
    getAssistantGrants.mockResolvedValue([activeGrant()]);
    getAssistantDestinations.mockResolvedValue([]);
    getUsers.mockResolvedValue([
      { id: 1, display_name: "Alice", friendly_name: "Alice" },
      { id: 2, display_name: "Ben", friendly_name: "Ben" },
    ]);
    listCollections.mockResolvedValue([{ id: 17, name: "Winter films" }]);
    getLibraries.mockResolvedValue([{ key: "2", title: "Films" }]);
    createAssistantGrant.mockResolvedValue(activeGrant());
    updateAssistantGrant.mockImplementation((_id, body) => Promise.resolve({ ...activeGrant(), ...body }));
    removeAssistantGrant.mockResolvedValue(undefined);
  });

  it("explains the benefit and preserves setup instructions when disabled", async () => {
    getAssistantStatus.mockResolvedValue({
      enabled: false,
      configuration_error: "SHORTLIST_MCP_URL is not configured.",
      configuration_hint: "Set the canonical MCP URL, then restart Shortlist.",
      presets: {},
      setting_groups: [],
    });
    renderPage();

    expect(await screen.findByRole("heading", { name: "AI assistants", level: 1 })).toBeVisible();
    expect(screen.getByText(/Connect ChatGPT, Claude or Codex to set up and manage Shortlist/)).toBeVisible();
    expect(screen.getByText(/You choose each connection’s permissions and limits/)).toBeVisible();
    expect(screen.getByRole("heading", { name: "AI assistants are off" })).toBeVisible();
    expect(screen.getByText("SHORTLIST_MCP_URL is not configured.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "New connection" })).not.toBeInTheDocument();
  });

  it("creates a connection with broad row and library access and zero paid calls by default", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    expect(screen.getByText(/All current and future people\. All current and future rows; all current and future libraries/)).toBeVisible();
    expect(screen.queryByRole("radio", { name: "Selected rows" })).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Max batch size")).not.toBeInTheDocument();
    expect(screen.getByLabelText("Allow this assistant to use paid services")).not.toBeChecked();
    expect(screen.getByRole("button", { name: "Manage Shortlist" })).toHaveAttribute("aria-pressed", "true");
    await user.type(screen.getByPlaceholderText("Living room Codex"), "Desktop Codex");
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      name: "Desktop Codex",
      preset: "owner_automation",
      capabilities: activeStatus.presets.owner_automation,
      expires_in_days: 90,
      constraints: expect.objectContaining({
        row_ids: [], library_keys: [], include_future_rows: true, include_future_libraries: true,
        setting_groups: ["schedule"], max_batch_size: 25, max_work_per_operation: null, max_provider_calls: 0,
      }),
    })));
  });

  it("keeps the paid amount visible and invalid while the owner replaces it", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.type(screen.getByPlaceholderText("Living room Codex"), "Paid client");
    await user.click(screen.getByLabelText("Allow this assistant to use paid services"));
    const allowance = screen.getByLabelText("Lifetime call allowance");
    expect(allowance).toHaveValue(1);
    await user.clear(allowance);
    expect(allowance).toBeVisible();
    expect(allowance).toBeInvalid();
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
    await user.type(allowance, "2");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      constraints: expect.objectContaining({ max_provider_calls: 2 }),
      capabilities: expect.arrayContaining(["ai.generate"]),
    })));
  });

  it("retains a selected endpoint snapshot if the live catalog changes before save", async () => {
    const user = userEvent.setup();
    const original = { service_id: "searxng", label: "SearXNG search", purposes: ["search"], host: "localhost", destination_id: "http://localhost:8080" };
    getAssistantDestinations.mockResolvedValue([original]);
    const client = renderPage();

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.type(screen.getByPlaceholderText("Living room Codex"), "Search client");
    await user.click(screen.getByRole("checkbox", { name: /SearXNG search/ }));
    await act(async () => {
      client.setQueryData(["assistant", "destinations"], [{ ...original, destination_id: "http://localhost:8081" }]);
    });
    await user.click(screen.getByRole("button", { name: "Create connection" }));

    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledWith(expect.objectContaining({
      constraints: expect.objectContaining({ destination_ids: ["http://localhost:8080"] }),
      selected_destinations: [{ service_id: "searxng", destination_id: "http://localhost:8080" }],
    })));
  });

  it("summarizes broad existing authority as all rows and libraries", async () => {
    getAssistantGrants.mockResolvedValue([activeGrant({
      constraints: { ...activeGrant().constraints, row_ids: [], library_keys: [], include_future_rows: true, include_future_libraries: true },
    })]);
    renderPage();

    expect(await screen.findByText(/All people · All rows · All libraries/)).toBeVisible();
  });

  it("resets a hidden expiry to 90 days for the next connection", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.type(screen.getByPlaceholderText("Living room Codex"), "First client");
    await user.click(screen.getByRole("button", { name: "Advanced access and limits" }));
    await user.clear(screen.getByLabelText("Connection expires after days"));
    await user.type(screen.getByLabelText("Connection expires after days"), "1");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledTimes(1));
    expect(createAssistantGrant).toHaveBeenNthCalledWith(1, expect.objectContaining({ expires_in_days: 1 }));

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    expect(screen.queryByLabelText("Connection expires after days")).not.toBeInTheDocument();
    await user.type(screen.getByPlaceholderText("Living room Codex"), "Second client");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledTimes(2));
    expect(createAssistantGrant).toHaveBeenNthCalledWith(2, expect.objectContaining({ expires_in_days: 90 }));
  });

  it("shows a retained custom expiry when reopening a cancelled draft", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.click(screen.getByRole("button", { name: "Advanced access and limits" }));
    await user.clear(screen.getByLabelText("Connection expires after days"));
    await user.type(screen.getByLabelText("Connection expires after days"), "30");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "New connection" }));

    expect(screen.getByRole("button", { name: "Hide advanced access and limits" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Connection expires after days")).toHaveValue(30);
  });

  it("shows an invalid retained limit and blocks submission after reopening a cancelled draft", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.type(screen.getByPlaceholderText("Living room Codex"), "Draft client");
    await user.click(screen.getByRole("button", { name: "Advanced access and limits" }));
    await user.clear(screen.getByLabelText("Max batch size"));
    await user.type(screen.getByLabelText("Max batch size"), "1001");
    await user.click(screen.getByRole("button", { name: "Cancel" }));
    await user.click(screen.getByRole("button", { name: "New connection" }));

    expect(screen.getByRole("button", { name: "Hide advanced access and limits" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Max batch size")).toHaveValue(1001);
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    expect(createAssistantGrant).not.toHaveBeenCalled();
    await user.clear(screen.getByLabelText("Max batch size"));
    await user.type(screen.getByLabelText("Max batch size"), "25");
    await user.click(screen.getByRole("button", { name: "Create connection" }));
    await waitFor(() => expect(createAssistantGrant).toHaveBeenCalledTimes(1));
  });

  it("keeps invalid advanced limits visible and focused until corrected", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.click(screen.getByRole("button", { name: "Advanced access and limits" }));
    const batch = screen.getByLabelText("Max batch size");
    const work = screen.getByLabelText("Max work per operation");
    const expiry = screen.getByLabelText("Connection expires after days");
    for (const [input, invalidValue] of [[batch, "1001"], [work, "100001"], [expiry, "0"]] as const) {
      await user.clear(input);
      await user.type(input, invalidValue);
      await user.click(screen.getByRole("button", { name: "Hide advanced access and limits" }));
      expect(screen.getByRole("button", { name: "Hide advanced access and limits" })).toHaveAttribute("aria-expanded", "true");
      expect(input).toHaveFocus();
      await user.clear(input);
      await user.type(input, "1");
    }
    await user.clear(work);
    await user.click(screen.getByRole("button", { name: "Hide advanced access and limits" }));
    expect(screen.queryByLabelText("Max work per operation")).not.toBeInTheDocument();
  });

  it("prefills an active grant, shows custom permissions, and saves only changed resources", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit connection" }));

    expect(screen.getByText("Custom access")).toBeVisible();
    expect(screen.queryByLabelText("Ben")).not.toBeInTheDocument();
    expect(screen.getByText(/all current and future people/i)).toBeVisible();
    expect(screen.getByText(/1 selected rows; 1 selected libraries/)).toBeVisible();
    expect(screen.getByRole("radio", { name: "Selected rows" })).toBeChecked();
    expect(screen.getByLabelText("Max batch size")).toHaveValue(25);
    expect(screen.getByText(/Previously approved service endpoints/)).toBeVisible();
    expect(screen.getByText(/instance.read · rows.update/)).toBeVisible();

    await user.click(screen.getByRole("radio", { name: "All current and future rows" }));
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("grant-codex", {
      expected_revision: 4,
      constraints: { include_future_rows: true },
      selected_destinations: [],
    }));
    expect(await screen.findByRole("status")).toHaveTextContent(/New requests use the changed access/);
  });

  it("keeps a restricted grant and unavailable resource IDs when editing an unrelated limit", async () => {
    const user = userEvent.setup();
    getAssistantGrants.mockResolvedValue([activeGrant({
      constraints: { ...activeGrant().constraints, row_ids: [17, 99], library_keys: ["2", "missing"] },
    })]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit connection" }));
    expect(screen.getByRole("button", { name: "Hide advanced access and limits" })).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByRole("radio", { name: "Selected rows" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Selected libraries" })).toBeChecked();
    await user.clear(screen.getByLabelText("Max batch size"));
    await user.type(screen.getByLabelText("Max batch size"), "26");
    await user.click(screen.getByRole("button", { name: "Hide advanced access and limits" }));
    expect(screen.queryByLabelText("Max work per operation")).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("grant-codex", {
      expected_revision: 4,
      constraints: { max_batch_size: 26 },
      selected_destinations: [],
    }));
  });

  it("shows an exhausted paid allowance and preserves it during an unrelated edit", async () => {
    const user = userEvent.setup();
    getAssistantGrants.mockResolvedValue([activeGrant({
      capabilities: [...activeGrant().capabilities, "ai.generate"],
      constraints: { ...activeGrant().constraints, max_provider_calls: 100 },
      provider_call_quota: { lifetime_limit: 100, reserved: 100, remaining: 0 },
    })]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit connection" }));
    expect(screen.getByLabelText("Allow this assistant to use paid services")).toBeChecked();
    expect(screen.getByLabelText("Lifetime call allowance")).toHaveValue(100);
    expect(screen.getByText(/Used or uncertain: 100 · Remaining now: 0 · Exhausted/)).toBeVisible();
    await user.clear(screen.getByLabelText("Max batch size"));
    await user.type(screen.getByLabelText("Max batch size"), "26");
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("grant-codex", {
      expected_revision: 4,
      constraints: { max_batch_size: 26 },
      selected_destinations: [],
    }));
  });

  it("does not offer a paid call when the lifetime maximum is already reserved", async () => {
    const user = userEvent.setup();
    getAssistantGrants.mockResolvedValue([activeGrant({
      constraints: { ...activeGrant().constraints, max_provider_calls: 0 },
      provider_call_quota: { lifetime_limit: 0, reserved: 100, remaining: 0 },
    })]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit connection" }));
    expect(screen.getByLabelText("Allow this assistant to use paid services")).toBeDisabled();
    expect(screen.getByText(/Used or uncertain: 100 · Remaining: 0/)).toBeVisible();
  });

  it("keeps an invalid edited work limit visible instead of hiding validation", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit connection" }));
    const work = screen.getByLabelText("Max work per operation");
    await user.clear(work);
    await user.type(work, "100001");
    await user.click(screen.getByRole("button", { name: "Hide advanced access and limits" }));
    expect(screen.getByRole("button", { name: "Hide advanced access and limits" })).toHaveAttribute("aria-expanded", "true");
    expect(work).toHaveFocus();
    expect(updateAssistantGrant).not.toHaveBeenCalled();
  });

  it("selects current resources in selected mode without enabling future access", async () => {
    const user = userEvent.setup();
    listCollections.mockResolvedValue([{ id: 17, name: "Winter films" }, { id: 18, name: "Summer films" }]);
    getLibraries.mockResolvedValue([{ key: "2", title: "Films" }, { key: "3", title: "Shows" }]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit connection" }));
    await user.click(screen.getByRole("button", { name: "Select all current rows" }));
    await user.click(screen.getByRole("button", { name: "Select all current libraries" }));
    expect(screen.getByLabelText("Summer films")).toBeChecked();
    expect(screen.getByLabelText("Shows")).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Clear current rows selection" }));
    expect(screen.getByRole("radio", { name: "Selected rows" })).toBeChecked();
    expect(screen.getByRole("radio", { name: "Selected libraries" })).toBeChecked();
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("grant-codex", {
      expected_revision: 4,
      constraints: { row_ids: [], library_keys: ["2", "3"] },
      selected_destinations: [],
    }));
  });

  it("selects only setting groups in bulk and retains hidden advanced defaults", async () => {
    const user = userEvent.setup();
    getAssistantStatus.mockResolvedValue({ ...activeStatus, setting_groups: ["schedule", "recommendations", "row_defaults"] });
    renderPage();

    await user.click(await screen.findByRole("button", { name: "New connection" }));
    await user.click(screen.getByRole("button", { name: "Advanced access and limits" }));
    await user.click(screen.getByRole("button", { name: "Select all settings groups" }));
    expect(screen.getByLabelText("Manage schedule settings")).toBeChecked();
    expect(screen.getByLabelText("Manage recommendations settings")).toBeChecked();
    expect(screen.getByLabelText("Manage row defaults settings")).toBeChecked();
    expect(screen.getByLabelText("Allow viewing context to approved services")).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "Clear settings groups selection" }));
    expect(screen.getByLabelText("Manage schedule settings")).not.toBeChecked();
    expect(screen.getByLabelText("Max batch size")).toHaveValue(25);
    expect(screen.getByLabelText("Max work per operation")).toHaveValue(null);
    expect(screen.getByLabelText("Connection expires after days")).toHaveValue(90);
  });

  it("discards pending resource choices when cancelled", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit connection" }));
    await user.click(screen.getByRole("radio", { name: "All current and future rows" }));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(updateAssistantGrant).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "Edit connection" })).toBeVisible();
  });

  it("requires an explicit reload after a stale revision conflict", async () => {
    const user = userEvent.setup();
    updateAssistantGrant.mockRejectedValue(new ApiError(409, "assistant grant changed or was revoked"));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit connection" }));
    await user.click(screen.getByRole("radio", { name: "All current and future rows" }));
    await user.click(screen.getByRole("button", { name: "Save connection" }));

    expect(await screen.findByText(/changed elsewhere/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Reload resources" }));
    expect(await screen.findByRole("button", { name: "Edit connection" })).toBeVisible();
    expect(getAssistantGrants).toHaveBeenCalledTimes(2);
  });

  it("does not offer resource editing for inactive grants", async () => {
    getAssistantGrants.mockResolvedValue([activeGrant({ revoked_at: "2026-10-01T00:00:00Z" })]);
    renderPage();

    expect(await screen.findByText("Revoked")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Edit connection" })).not.toBeInTheDocument();
  });

  it("makes legacy access approval visible before opening its explicit confirmation", async () => {
    const user = userEvent.setup();
    getAssistantGrants.mockResolvedValue([activeGrant({ requires_access_approval: true })]);
    renderPage();

    expect(await screen.findByText("Approval required")).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Approve updated access" }));
    expect(screen.getByRole("heading", { name: "Approve updated access for Seasonal Codex" })).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Confirm updated access" }));

    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("grant-codex", {
      expected_revision: 4,
      approve_updated_access: true,
    }));
  });

  it("removes only a revoked connection after confirmation while retaining its activity history", async () => {
    const user = userEvent.setup();
    getAssistantGrants.mockResolvedValue([activeGrant({ revoked_at: "2026-10-01T00:00:00Z" })]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Remove" }));
    expect(screen.getByRole("heading", { name: "Remove revoked connection?" })).toBeVisible();
    expect(screen.getByText(/activity history is retained/i)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Keep connection" }));
    expect(removeAssistantGrant).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Remove" }));
    await user.click(screen.getByRole("button", { name: "Remove connection" }));
    await waitFor(() => expect(removeAssistantGrant).toHaveBeenCalledWith("grant-codex"));
    expect(getAssistantGrants).toHaveBeenCalledTimes(2);
  });

  it("keeps a revoked connection visible when removal fails", async () => {
    const user = userEvent.setup();
    removeAssistantGrant.mockRejectedValue(new ApiError(500, "Could not remove the connection."));
    getAssistantGrants.mockResolvedValue([activeGrant({ revoked_at: "2026-10-01T00:00:00Z" })]);
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Remove" }));
    await user.click(screen.getByRole("button", { name: "Remove connection" }));

    expect(await screen.findByText("Could not remove the connection.")).toBeVisible();
    expect(screen.getByText("Revoked")).toBeVisible();
  });

  it("keeps a future offset expiry editable and treats equal or past expiries as inactive", async () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date("2027-01-03T05:00:00Z"));
    getAssistantGrants.mockResolvedValue([
      activeGrant({ id: "future", name: "Future offset", expires_at: "2027-01-03T15:01:00+10:00" }),
      activeGrant({ id: "equal", name: "Equal offset", expires_at: "2027-01-03T15:00:00+10:00" }),
      activeGrant({ id: "past", name: "Past offset", expires_at: "2027-01-03T14:59:00+10:00" }),
    ]);
    renderPage();

    const future = (await screen.findByText("Future offset")).closest("article")!;
    const equal = screen.getByText("Equal offset").closest("article")!;
    const past = screen.getByText("Past offset").closest("article")!;
    expect(within(future).getByRole("button", { name: "Edit connection" })).toBeVisible();
    expect(within(equal).queryByRole("button", { name: "Edit connection" })).not.toBeInTheDocument();
    expect(within(past).queryByRole("button", { name: "Edit connection" })).not.toBeInTheDocument();
  });
});
