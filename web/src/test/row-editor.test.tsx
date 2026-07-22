import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowEditor } from "@/components/rows/row-editor";
import type * as ApiModule from "@/lib/api";
import type { Collection, User } from "@/lib/types";

const { updateCollection } = vi.hoisted(() => ({
  updateCollection: vi.fn((id: number, body: unknown) =>
    Promise.resolve({ ...(body as object), id }),
  ),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      updateCollection: (id: number, body: unknown) =>
        updateCollection(id, body),
      getSettings: () => Promise.resolve({}),
      getLibraries: () => Promise.resolve([]),
      getImageProvider: () =>
        Promise.resolve({ capable: false, provider: "", reason: "" }),
    },
  };
});

function row(patch: Partial<Collection> = {}): Collection {
  return {
    id: 1,
    slug: "hidden-gems",
    name: "Hidden Gems",
    last_run_id: null,
    build: "per_person",
    audience: "everyone",
    audience_user_ids: [],
    enabled: true,
    schedule: "30 3 * * *",
    size: 15,
    media: "both",
    sort_order: 0,
    name_template: "",
    min_watchers: 2,
    request_tag: "",
    candidate_sources: [],
    library_keys: [],
    watched_pct: null,
    freshness: null,
    recent_count: null,
    placement: "both",
    pin_top: false,
    hub_anchor: {},
    prompt: { tone: "", guidance: "", template: "" },
    poster: { mode: "", title: "", subtitle: "", style: "", has_image: false },
    ...patch,
  };
}

function user(patch: Partial<User> = {}): User {
  return {
    id: 1,
    username: "sarah",
    slug: "sarah",
    user_type: "shared",
    enabled: true,
    cold_start: false,
    history_depth: 10,
    last_run_at: null,
    request_tag: "",
    hit_rate: null,
    ...patch,
  };
}

function renderEditor(collection: Collection, users: User[] = []) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <RowEditor collection={collection} users={users} onClose={() => {}} />
    </QueryClientProvider>,
  );
}

describe("RowEditor — already-watched titles", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the watched slider when a row overrides the global cap", () => {
    renderEditor(row({ watched_pct: 0.25 }));
    const slider = screen.getByRole("slider", {
      name: /already-watched/i,
    });
    expect(slider).toHaveValue("25");
    // The "use the global default" switch is OFF when the row sets its own cap.
    expect(
      screen.getByRole("switch", { name: /global already-watched default/i }),
    ).not.toBeChecked();
  });

  it("hides the slider and checks the switch when the row inherits the global cap", () => {
    renderEditor(row({ watched_pct: null }));
    expect(
      screen.queryByRole("slider", { name: /already-watched/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByRole("switch", { name: /global already-watched default/i }),
    ).toBeChecked();
  });

  it("round-trips a per-row watched cap into the PATCH body", async () => {
    renderEditor(row({ watched_pct: null }));

    // Turn off "use global default" to reveal the slider (starts at 0%).
    await userEvent.click(
      screen.getByRole("switch", { name: /global already-watched default/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const call = updateCollection.mock.calls.at(0);
    expect(call?.[0]).toBe(1);
    expect((call?.[1] as Collection).watched_pct).toBe(0);
  });
});

describe("RowEditor — placement", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("reflects the saved placement as the pressed chip", () => {
    renderEditor(row({ placement: "library" }));
    expect(
      screen.getByRole("button", { name: "Library only" }),
    ).toHaveAttribute("aria-pressed", "true");
  });

  it("round-trips a changed placement into the PATCH body", async () => {
    renderEditor(row({ placement: "both" }));

    await userEvent.click(screen.getByRole("button", { name: "Home only" }));
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    const body = updateCollection.mock.calls.at(0)?.[1] as Collection;
    expect(body.placement).toBe("home");
  });
});

describe("RowEditor — freshness", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the freshness slider only when the row overrides the global default", () => {
    renderEditor(row({ freshness: 0.25 }));
    expect(
      screen.getByRole("slider", { name: /how often the row refreshes/i }),
    ).toHaveValue("25");
    expect(
      screen.getByRole("switch", { name: /global freshness default/i }),
    ).not.toBeChecked();
  });

  it("round-trips a per-row freshness into the PATCH body", async () => {
    renderEditor(row({ freshness: null }));

    await userEvent.click(
      screen.getByRole("switch", { name: /global freshness default/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).freshness,
    ).toBe(0);
  });
});

describe("RowEditor — recent watches to search", () => {
  beforeEach(() => {
    updateCollection.mockClear();
  });

  it("shows the number field only when the row overrides the global default", () => {
    renderEditor(row({ recent_count: 5 }));
    expect(screen.getByLabelText(/Recent watches to search/i)).toHaveValue(5);
    expect(
      screen.getByRole("switch", { name: /global recent-watches default/i }),
    ).not.toBeChecked();
  });

  it("round-trips a per-row recent_count into the PATCH body", async () => {
    renderEditor(row({ recent_count: null }));

    await userEvent.click(
      screen.getByRole("switch", { name: /global recent-watches default/i }),
    );
    await userEvent.click(
      screen.getByRole("button", { name: /Save changes/i }),
    );

    await waitFor(() => expect(updateCollection).toHaveBeenCalled());
    expect(
      (updateCollection.mock.calls.at(0)?.[1] as Collection).recent_count,
    ).toBe(10);
  });
});


describe("RowEditor — a shared row that can never build", () => {
  const sharedRow = (patch: Partial<Collection> = {}) =>
    row({ build: "shared", min_watchers: 2, ...patch });
  const warning = () =>
    screen.queryByText(/This row can’t build yet/i);

  it("warns when only one person in the audience is active in runs", () => {
    // The exact shape of issue #3: a shared row on a server with one enabled user can never reach
    // its 2-watcher floor, so it silently reports "skipped" every night forever.
    renderEditor(sharedRow(), [
      user({ id: 1, username: "sarah" }),
      user({ id: 2, username: "mike", enabled: false }),
    ]);
    expect(warning()).toBeInTheDocument();
    expect(screen.getByText(/only 1 of them is active in runs/i)).toBeInTheDocument();
  });

  it("counts a PAUSED user as inactive — the engine drops them before any row is built", () => {
    renderEditor(sharedRow(), [
      user({ id: 1, username: "sarah" }),
      user({ id: 2, username: "mike", prefs: { paused: true } }),
    ]);
    expect(warning()).toBeInTheDocument();
  });

  it("says nobody rather than 'only 0' when the audience is empty", () => {
    renderEditor(sharedRow({ audience: "subset", audience_user_ids: [] }), [
      user({ id: 1, username: "sarah" }),
      user({ id: 2, username: "mike" }),
    ]);
    expect(screen.getByText(/nobody in its audience is active in runs/i)).toBeInTheDocument();
  });

  it("stays quiet once the row can actually build", () => {
    renderEditor(sharedRow(), [
      user({ id: 1, username: "sarah" }),
      user({ id: 2, username: "mike" }),
    ]);
    expect(warning()).toBeNull();
  });

  it("stays quiet on a per-person row, which has no watcher floor at all", () => {
    renderEditor(row({ build: "per_person" }), [user({ id: 1 })]);
    expect(warning()).toBeNull();
  });
});
