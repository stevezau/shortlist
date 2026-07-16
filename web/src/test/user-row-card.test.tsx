import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UserRowsSection } from "@/components/user-detail/user-row-card";
import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { RowOverridePatch, User, UserRow } from "@/lib/types";

const { getUserRows, setUserRowOverride } = vi.hoisted(() => ({
  getUserRows: vi.fn(),
  setUserRowOverride: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUserRows: (id: number) => getUserRows(id),
      setUserRowOverride: (
        id: number,
        collectionId: number,
        patch: RowOverridePatch,
      ) => setUserRowOverride(id, collectionId, patch),
      previewPrompt: () => Promise.resolve({ system: "", user: "" }),
    },
  };
});

const USER: User = {
  id: 7,
  username: "sarah",
  slug: "sarah",
  user_type: "shared",
  enabled: true,
  cold_start: false,
  history_depth: 40,
  last_run_at: null,
  request_tag: "",
  hit_rate: null,
};

function row(patch: Partial<UserRow> = {}): UserRow {
  return {
    collection_id: 3,
    slug: "picked",
    name: "Picked for You",
    media: "both",
    size: 15,
    is_default: true,
    muted: false,
    override: {
      row_size: null,
      prompt_tone: "",
      prompt_guidance: "",
      prompt_template: "",
    },
    picks: [],
    ...patch,
  };
}

function renderSection() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UserRowsSection user={USER} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const muteSwitch = () =>
  screen.getByRole("switch", {
    name: /Show Picked for You for this person/i,
  });

describe("UserRowCard", () => {
  beforeEach(() => {
    getUserRows.mockReset();
    setUserRowOverride.mockReset();
  });

  it("does not leave the card reading 'muted' when the mute is rejected", async () => {
    getUserRows.mockResolvedValue([row({ muted: false })]);
    setUserRowOverride.mockRejectedValue(
      new ApiError(502, "Plex did not accept the change."),
    );
    renderSection();
    await waitFor(() => expect(muteSwitch()).toBeChecked());

    await userEvent.click(muteSwitch());

    // The card is a privacy claim. If the PUT failed, the row is STILL delivered to this person,
    // so the card must not dim, badge itself "muted", or leave the switch off.
    expect(await screen.findByRole("alert")).toHaveTextContent(
      /still showing for this person/i,
    );
    expect(screen.queryByText("muted")).toBeNull();
    await waitFor(() => expect(muteSwitch()).toBeChecked());
  });

  it("shows the row as muted once the server has actually accepted it", async () => {
    getUserRows.mockResolvedValueOnce([row({ muted: false })]);
    getUserRows.mockResolvedValue([row({ muted: true })]);
    setUserRowOverride.mockResolvedValue({});
    renderSection();
    await waitFor(() => expect(muteSwitch()).toBeChecked());

    await userEvent.click(muteSwitch());

    expect(await screen.findByText("muted")).toBeTruthy();
    expect(setUserRowOverride).toHaveBeenCalledWith(7, 3, { muted: true });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("auto-saves the customization drawer — there is no Save button to miss", async () => {
    getUserRows.mockResolvedValue([row()]);
    setUserRowOverride.mockResolvedValue({});
    renderSection();

    await userEvent.click(
      await screen.findByRole("button", { name: /Customize for this person/i }),
    );
    expect(screen.queryByRole("button", { name: /^Save$/ })).toBeNull();

    await userEvent.click(
      screen.getByRole("switch", { name: /Custom row size/i }),
    );
    const sizeInput = screen.getByLabelText(/Titles for this person/i);
    await userEvent.clear(sizeInput);
    await userEvent.type(sizeInput, "20");
    await userEvent.tab(); // blur commits the typed size

    // Collapsing the drawer used to throw this away; it now persists on its own.
    await waitFor(() => expect(setUserRowOverride).toHaveBeenCalled(), {
      timeout: 3000,
    });
    const call = setUserRowOverride.mock.calls.at(-1);
    expect(call?.[1]).toBe(3);
    expect(call?.[2]).toMatchObject({ row_size: 20 });
    // The drawer must never carry the mute flag — that would let a stale switch value ride along.
    expect(call?.[2]).not.toHaveProperty("muted");
  });

  it("offers a retry when a customization auto-save fails, instead of stranding the edit", async () => {
    getUserRows.mockResolvedValue([row()]);
    setUserRowOverride.mockRejectedValue(
      new ApiError(500, "Database is busy."),
    );
    renderSection();

    await userEvent.click(
      await screen.findByRole("button", { name: /Customize for this person/i }),
    );
    await userEvent.click(
      screen.getByRole("switch", { name: /Custom row size/i }),
    );
    const sizeInput = screen.getByLabelText(/Titles for this person/i);
    await userEvent.clear(sizeInput);
    await userEvent.type(sizeInput, "10");
    await userEvent.tab(); // blur commits the typed size

    expect(await screen.findByRole("alert")).toHaveTextContent(
      /Database is busy/i,
    );
    setUserRowOverride.mockResolvedValue({});
    await userEvent.click(screen.getByRole("button", { name: /Try again/i }));

    await waitFor(() =>
      expect(setUserRowOverride.mock.calls.at(-1)?.[2]).toMatchObject({
        row_size: 10,
      }),
    );
  });
});
