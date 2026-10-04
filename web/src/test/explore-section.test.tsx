import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ExploreSection } from "@/components/rows/explore-section";
import { ApiError } from "@/lib/api";
import type * as ApiModule from "@/lib/api";
import { blankInput } from "@/lib/collections";
import type { Collection, CollectionInput, ThemePreview, ThemeRotation } from "@/lib/types";

const api = vi.hoisted(() => ({
  getThemeRotation: vi.fn(),
  setUpNext: vi.fn(),
  regenerateUpNext: vi.fn(),
  updateTheme: vi.fn(),
  previewTheme: vi.fn(),
  getThemeCapabilities: vi.fn(),
  getThemePrompts: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, ...api } };
});

function ref(id: number, name: string, patch = {}) {
  return { theme_id: id, name, emoji: null, started_at: "2026-09-01T00:00:00Z", due_at: null, ...patch };
}

function rotation(patch: Partial<ThemeRotation["targets"][number]> = {}): ThemeRotation {
  return {
    mode: "explore",
    days: 7,
    targets: [
      {
        user_id: 3,
        name: "Sarah",
        current: ref(1, "Heist films"),
        next: ref(2, "Slow-burn mysteries", { started_at: "2026-10-08T00:00:00Z" }),
        started_at: "2026-10-01T00:00:00Z",
        next_due_at: "2026-10-08T12:00:00Z",
        history: [ref(7, "Space operas"), ref(8, "Courtroom drama")],
        ...patch,
      },
    ],
  };
}

function savedRow(patch: Partial<Collection> = {}): Collection {
  return { id: 9, theme_id: 1, theme_mode: "explore", ai_paused: false, ...patch } as unknown as Collection;
}

function preview(): ThemePreview {
  return {
    draft: {
      id: null,
      slug: "x",
      name: "Slow-burn mysteries, shorter",
      emoji: null,
      brief: "b",
      origin: "ai",
      media: ["movie"],
      tags: [],
      genres: ["mystery"],
      excluded_genres: [],
      collections: [],
      picks: [],
      rules: {},
      content_hash: "h",
      ai_tokens: 0,
      stats: {},
    },
    stats: { named: 10, resolved: 9, in_library: 8, after_rules: 7, unwatched_median: null, truncated: false },
    diff: {
      added: ["Gone Girl"],
      removed: ["Se7en"],
      unchanged: [],
      added_count: 1,
      removed_count: 1,
      before_count: 5,
      after_count: 5,
      tags_added: [],
      tags_removed: [],
      genres_added: [],
      genres_removed: [],
      rules_changed: false,
    },
    tokens: 250,
  } as unknown as ThemePreview;
}

const changes = vi.fn();

function Harness({ collection, start = blankInput() }: { collection: Collection | null; start?: CollectionInput }) {
  const [input, setInput] = useState(start);
  return (
    <ExploreSection
      input={input}
      collection={collection}
      onChange={(patch) => {
        changes(patch);
        setInput((prev) => ({ ...prev, ...patch }));
      }}
    />
  );
}

function renderSection(collection: Collection | null, start?: CollectionInput) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness collection={collection} start={start} />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
  api.getThemeCapabilities.mockResolvedValue({ ai: true });
  api.getThemePrompts.mockResolvedValue({ guidance: "default guidance", mechanics: "m" });
});

describe("ExploreSection", () => {
  it("shows the days and the brief once Explore is chosen", async () => {
    renderSection(null);

    expect(screen.queryByLabelText("What kinds of lists should it pick? (optional)")).toBeNull();
    await userEvent.click(screen.getByRole("radio", { name: /Pick a new theme every/ }));

    expect(changes).toHaveBeenLastCalledWith({ theme_mode: "explore" });
    expect(screen.getByLabelText("Days each theme lasts")).toHaveValue(7);
    expect(screen.getByLabelText("What kinds of lists should it pick? (optional)")).toHaveAttribute(
      "placeholder",
      "Leave blank and each person gets a theme chosen from what they watch.",
    );
    expect(screen.getByText(/It uses your AI provider once per person per change\./)).toBeInTheDocument();
  });

  it("writes the days and the brief into the form", async () => {
    renderSection(null, { ...blankInput(), theme_mode: "explore" });

    const days = screen.getByLabelText("Days each theme lasts");
    await userEvent.clear(days);
    await userEvent.type(days, "14");
    expect(changes).toHaveBeenLastCalledWith({ theme_days: 14 });
    await userEvent.type(screen.getByLabelText("What kinds of lists should it pick? (optional)"), "x");
    expect(changes).toHaveBeenLastCalledWith({ explore_brief: "x" });
  });

  it("goes back to fixed and hides the days", async () => {
    renderSection(null, { ...blankInput(), theme_mode: "explore" });

    await userEvent.click(screen.getByRole("radio", { name: "Keep the same theme" }));

    expect(changes).toHaveBeenLastCalledWith({ theme_mode: "fixed" });
    expect(screen.queryByLabelText("Days each theme lasts")).toBeNull();
  });

  it("asks for a save before a new row has any rotation to show", () => {
    renderSection(null, { ...blankInput(), theme_mode: "explore" });

    expect(screen.getByText(/Save the row to see each person’s themes/)).toBeInTheDocument();
    expect(api.getThemeRotation).not.toHaveBeenCalled();
  });

  it("says the daily job picks the first theme", () => {
    renderSection(null, { ...blankInput(), theme_mode: "explore" });

    expect(
      screen.getByText(/The first one is picked by the daily theme job once this row is on\./),
    ).toBeInTheDocument();
  });

  it("says a person has no theme yet instead of a start date of nothing", async () => {
    api.getThemeRotation.mockResolvedValue(rotation({ current: null, started_at: null, next_due_at: null }));
    renderSection(savedRow(), { ...blankInput(), theme_mode: "explore" });

    const card = await screen.findByRole("region", { name: "Sarah" });
    expect(within(card).getByText("No theme yet")).toBeInTheDocument();
    expect(within(card).getByText("Up next: Slow-burn mysteries")).toBeInTheDocument();
    expect(card.textContent).not.toContain("starts —");
  });

  it("shows a skeleton while the rotation loads", () => {
    api.getThemeRotation.mockReturnValue(new Promise(() => {}));
    renderSection(savedRow(), { ...blankInput(), theme_mode: "explore" });

    expect(screen.getByRole("status", { name: "Reading each person’s themes" })).toBeInTheDocument();
  });

  it("shows an error with a retry when the rotation can't be read", async () => {
    api.getThemeRotation.mockRejectedValueOnce(new ApiError(500, "Couldn’t read the rotation."));
    api.getThemeRotation.mockResolvedValue(rotation());
    renderSection(savedRow(), { ...blankInput(), theme_mode: "explore" });

    expect(await screen.findByText("Couldn’t read the rotation.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Try again/ }));
    expect(await screen.findByText(/Up next: Slow-burn mysteries/)).toBeInTheDocument();
  });

  it("says nothing is queued yet when a person has no Up next", async () => {
    api.getThemeRotation.mockResolvedValue(rotation({ next: null, history: [] }));
    renderSection(savedRow(), { ...blankInput(), theme_mode: "explore" });

    expect(
      await screen.findByText("Nothing queued yet. The next theme is built a day before it starts."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Recent themes")).toBeNull();
  });

  it("shows each person's Up next and their recent themes", async () => {
    api.getThemeRotation.mockResolvedValue(rotation());
    renderSection(savedRow(), { ...blankInput(), theme_mode: "explore" });

    const card = await screen.findByRole("region", { name: "Sarah" });
    expect(within(card).getByText(/Up next: Slow-burn mysteries — starts /)).toBeInTheDocument();
    expect(within(card).getByText("Recent themes")).toBeInTheDocument();
    expect(within(card).getByText(/Space operas/)).toBeInTheDocument();
    expect(within(card).getByText(/Courtroom drama/)).toBeInTheDocument();
    expect(within(card).getByText("Shortlist won’t pick these again soon.")).toBeInTheDocument();
  });

  it("asks the AI for another theme", async () => {
    api.getThemeRotation.mockResolvedValue(rotation());
    api.regenerateUpNext.mockResolvedValue(ref(4, "New"));
    renderSection(savedRow(), { ...blankInput(), theme_mode: "explore" });

    await userEvent.click(await screen.findByRole("button", { name: "Pick another" }));

    await waitFor(() => expect(api.regenerateUpNext).toHaveBeenCalledWith(9, 3));
  });

  it("disables Pick another and Change it with a reason while the row's AI is paused", async () => {
    api.getThemeRotation.mockResolvedValue(rotation());
    renderSection(savedRow({ ai_paused: true }), { ...blankInput(), theme_mode: "explore" });

    expect(await screen.findByRole("button", { name: "Pick another" })).toBeDisabled();
    expect(screen.getAllByText(/AI is paused for this row/).length).toBeGreaterThan(0);
  });

  it("saves a changed theme, then points Up next at it", async () => {
    api.getThemeRotation.mockResolvedValue(rotation());
    api.previewTheme.mockResolvedValue(preview());
    api.updateTheme.mockResolvedValue({ ...preview().draft, id: 2 });
    api.setUpNext.mockResolvedValue(ref(2, "Slow-burn mysteries, shorter"));
    renderSection(savedRow(), { ...blankInput(), theme_mode: "explore" });

    await userEvent.click(await screen.findByRole("button", { name: "Change it" }));
    await userEvent.type(screen.getByLabelText("What should change?"), "shorter films");
    await userEvent.click(screen.getByRole("button", { name: "Preview the change" }));
    expect(api.previewTheme).toHaveBeenCalledWith(
      expect.objectContaining({ change: "shorter films", current_theme_id: 2, collection_id: 9 }),
    );
    expect(await screen.findByText("Gone Girl")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Use this theme" }));

    await waitFor(() => expect(api.setUpNext).toHaveBeenCalledWith(9, 3, 2));
    const [id, body] = api.updateTheme.mock.calls[0] as [number, { collection_id: number; tokens: number; draft: { name: string } }];
    expect(id).toBe(2);
    expect(body.collection_id).toBe(9);
    expect(body.tokens).toBe(250);
    expect(body.draft.name).toBe("Slow-burn mysteries, shorter");
    expect(api.updateTheme.mock.invocationCallOrder[0] ?? 0).toBeLessThan(api.setUpNext.mock.invocationCallOrder[0] ?? 0);
  });

  it("doesn't point Up next anywhere when the theme save fails", async () => {
    api.getThemeRotation.mockResolvedValue(rotation());
    api.previewTheme.mockResolvedValue(preview());
    api.updateTheme.mockRejectedValue(new ApiError(422, "That theme clashes with another."));
    renderSection(savedRow(), { ...blankInput(), theme_mode: "explore" });

    await userEvent.click(await screen.findByRole("button", { name: "Change it" }));
    await userEvent.type(screen.getByLabelText("What should change?"), "shorter films");
    await userEvent.click(screen.getByRole("button", { name: "Preview the change" }));
    await userEvent.click(await screen.findByRole("button", { name: "Use this theme" }));

    expect(await screen.findByText("That theme clashes with another.")).toBeInTheDocument();
    expect(api.setUpNext).not.toHaveBeenCalled();
  });
});
