import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
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
  getUsers,
  listCollections,
  getLibraries,
  updateAssistantGrant,
  removeAssistantGrant,
} = vi.hoisted(() => ({
  getAssistantStatus: vi.fn(),
  getAssistantGrants: vi.fn(),
  getUsers: vi.fn(),
  listCollections: vi.fn(),
  getLibraries: vi.fn(),
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
      getUsers,
      listCollections,
      getLibraries,
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
    manage_selected_rows: ["instance.read", "rows.write"],
    owner_automation: ["instance.read", "rows.write", "runs.write"],
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
    capabilities: ["instance.read", "rows.write"],
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
}

describe("AI assistants page", () => {
  afterEach(() => vi.useRealTimers());

  beforeEach(() => {
    getAssistantStatus.mockReset();
    getAssistantGrants.mockReset();
    getUsers.mockReset();
    listCollections.mockReset();
    getLibraries.mockReset();
    updateAssistantGrant.mockReset();
    removeAssistantGrant.mockReset();
    getAssistantStatus.mockResolvedValue(activeStatus);
    getAssistantGrants.mockResolvedValue([activeGrant()]);
    getUsers.mockResolvedValue([
      { id: 1, display_name: "Alice", friendly_name: "Alice" },
      { id: 2, display_name: "Ben", friendly_name: "Ben" },
    ]);
    listCollections.mockResolvedValue([{ id: 17, name: "Winter films" }]);
    getLibraries.mockResolvedValue([{ key: "2", title: "Films" }]);
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

  it("prefills an active grant, shows custom permissions, and saves only changed resources", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit resources" }));

    expect(screen.getByText("Custom permissions")).toBeVisible();
    expect(screen.queryByLabelText("Ben")).not.toBeInTheDocument();
    expect(screen.getByText(/all current and future people/i)).toBeVisible();
    expect(screen.getByLabelText("Max batch size")).toHaveValue(25);
    expect(screen.getByLabelText("Approved destination IDs or canonical URLs")).toHaveValue("https://search.example");
    expect(screen.getByText("instance.read")).toBeVisible();
    expect(screen.getByText("rows.write")).toBeVisible();

    await user.click(screen.getByLabelText("Include rows created later"));
    await user.click(screen.getByRole("button", { name: "Save resources" }));

    await waitFor(() => expect(updateAssistantGrant).toHaveBeenCalledWith("grant-codex", {
      expected_revision: 4,
      constraints: { include_future_rows: true },
    }));
    expect(await screen.findByRole("status")).toHaveTextContent(/New requests use the changed access/);
  });

  it("discards pending resource choices when cancelled", async () => {
    const user = userEvent.setup();
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit resources" }));
    await user.click(screen.getByLabelText("Include rows created later"));
    await user.click(screen.getByRole("button", { name: "Cancel" }));

    expect(updateAssistantGrant).not.toHaveBeenCalled();
    expect(await screen.findByRole("button", { name: "Edit resources" })).toBeVisible();
  });

  it("requires an explicit reload after a stale revision conflict", async () => {
    const user = userEvent.setup();
    updateAssistantGrant.mockRejectedValue(new ApiError(409, "assistant grant changed or was revoked"));
    renderPage();

    await user.click(await screen.findByRole("button", { name: "Edit resources" }));
    await user.click(screen.getByLabelText("Include rows created later"));
    await user.click(screen.getByRole("button", { name: "Save resources" }));

    expect(await screen.findByText(/changed elsewhere/)).toBeVisible();
    await user.click(screen.getByRole("button", { name: "Reload resources" }));
    expect(await screen.findByRole("button", { name: "Edit resources" })).toBeVisible();
    expect(getAssistantGrants).toHaveBeenCalledTimes(2);
  });

  it("does not offer resource editing for inactive grants", async () => {
    getAssistantGrants.mockResolvedValue([activeGrant({ revoked_at: "2026-10-01T00:00:00Z" })]);
    renderPage();

    expect(await screen.findByText("Revoked")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Edit resources" })).not.toBeInTheDocument();
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
    expect(within(future).getByRole("button", { name: "Edit resources" })).toBeVisible();
    expect(within(equal).queryByRole("button", { name: "Edit resources" })).not.toBeInTheDocument();
    expect(within(past).queryByRole("button", { name: "Edit resources" })).not.toBeInTheDocument();
  });
});
