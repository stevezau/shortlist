import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowEditor } from "@/components/rows/row-editor";
import { ApiError } from "@/lib/api";
import type * as ApiModule from "@/lib/api";
import { blankInput, toInput } from "@/lib/collections";
import { findRowTemplate } from "@/lib/row-templates";
import type { Collection, User } from "@/lib/types";
import { BUILTINS } from "@/test/season-fixtures";

const api = vi.hoisted(() => ({
  createCollection: vi.fn(),
  updateCollection: vi.fn(),
  createTheme: vi.fn(),
  updateTheme: vi.fn(),
  previewTheme: vi.fn(),
  getThemeCapabilities: vi.fn(),
  getThemePrompts: vi.fn(),
  getTheme: vi.fn(),
  getRun: vi.fn(),
  startRun: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      ...api,
      getSettings: () => Promise.resolve({}),
      getLibraries: () => Promise.resolve([]),
      getSeasons: () => Promise.resolve(BUILTINS),
      getSeasonPresets: () => Promise.resolve([]),
      getImageProvider: () => Promise.resolve({ capable: false, provider: "", reason: "" }),
      getSchedule: () => Promise.reject(new Error("no schedule")),
      getPrivacyStatus: () => Promise.reject(new Error("no privacy read")),
      getCollectionEffectiveness: () => Promise.reject(new Error("no history")),
    },
  };
});

const THEME = {
  id: null,
  slug: "twist-endings",
  name: "Twist endings",
  emoji: "🌀",
  brief: "films with a twist",
  origin: "ai",
  media: ["movie"],
  tags: [{ id: 111, name: "twist ending" }],
  genres: ["thriller"],
  excluded_genres: [],
  collections: [],
  picks: [{ tmdb_id: 1, media: "movie", origin: "ai", reason: "The twist", title: "Se7en", year: 1995 }],
  rules: { max_runtime: 140 },
  content_hash: "h",
  ai_tokens: 0,
  stats: {},
};

const PREVIEW = {
  draft: THEME,
  stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25, unwatched_median: null },
  diff: null,
  tokens: 321,
};

function aiRow(patch: Partial<Collection> = {}): Collection {
  const base = toInput({ ...blankInput(), id: 9, slug: "twist-endings" } as unknown as Collection);
  return {
    ...base,
    id: 9,
    slug: "twist-endings",
    last_run_id: null,
    preview_titles: [],
    name: "{theme_emoji} {theme}",
    name_template: "{theme_emoji} {theme}",
    enabled: false,
    theme_id: 5,
    ai_paused: false,
    ai_tokens: 400,
    ai_instructions: { mode: "default", text: "" },
    poster: { mode: "", title: "", subtitle: "", style: "", has_image: false },
    shown_today: true,
    season_status: null,
    ...patch,
  } as unknown as Collection;
}

const users: User[] = [];

function renderEditor(props: { collection: Collection | null; template?: ReturnType<typeof findRowTemplate> }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const onClose = vi.fn();
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <RowEditor collection={props.collection} template={props.template ?? null} users={users} onClose={onClose} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return { onClose };
}

async function buildAList(brief = "films with a twist") {
  await userEvent.type(await screen.findByLabelText("Describe it"), brief);
  await userEvent.click(screen.getByRole("button", { name: "Build the list" }));
  await screen.findByRole("region", { name: /the list/i });
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  api.getThemeCapabilities.mockResolvedValue({ ai: true });
  api.getThemePrompts.mockResolvedValue({ guidance: "You curate themed rows.", mechanics: "Reply with JSON." });
  api.previewTheme.mockResolvedValue(PREVIEW);
  api.getTheme.mockResolvedValue({ ...THEME, id: 5, ai_tokens: 400, stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25 } });
  api.createTheme.mockResolvedValue({ ...THEME, id: 55 });
  api.updateTheme.mockResolvedValue({ ...THEME, id: 5 });
  api.createCollection.mockResolvedValue({ id: 1 });
  api.updateCollection.mockResolvedValue({ id: 9 });
});

describe("a new AI row", () => {
  const template = findRowTemplate("describe-a-row");

  it("is a fixed AI row type with its own sections, and says it starts off", async () => {
    renderEditor({ collection: null, template });

    expect(await screen.findByText(/Row type: AI row/)).toBeInTheDocument();
    expect(screen.queryByText("Change row type…")).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Try it" })).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "AI prompts" })).toBeInTheDocument();
    expect(screen.getByText(/starts switched off/i)).toBeInTheDocument();
  });

  it("has no request settings, because an AI row only picks from what the server has", async () => {
    renderEditor({ collection: null, template });
    await screen.findByText(/Row type: AI row/);

    expect(screen.queryByRole("link", { name: "Requests" })).not.toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Requests" })).not.toBeInTheDocument();
  });

  it("can't be added until its list is built", async () => {
    renderEditor({ collection: null, template });

    expect(await screen.findByText("Build the row’s list before adding it.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add row" })).toBeDisabled();
  });

  it("saves the list as a theme, then the row following it, switched off, with the tokens it cost", async () => {
    renderEditor({ collection: null, template });
    await buildAList();

    await userEvent.click(screen.getByRole("button", { name: "Add row" }));

    await waitFor(() => expect(api.createCollection).toHaveBeenCalled());
    expect(api.createTheme).toHaveBeenCalledWith({
      draft: expect.objectContaining({ name: "Twist endings", origin: "ai", media: ["movie"] }),
      tokens: 321,
      collection_id: null,
      stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25 },
    });
    const body = api.createCollection.mock.calls[0]?.[0] as Record<string, unknown>;
    expect(body).toMatchObject({ theme_id: 55, enabled: false, build: "per_person", name: "{theme_emoji} {theme}" });
  });

  it("replaces the theme it already saved, rather than making another, when the row save fails and is retried", async () => {
    api.createCollection.mockRejectedValueOnce(new ApiError(422, "A row with that name already exists."));
    api.updateTheme.mockResolvedValue({ ...THEME, id: 55 });
    renderEditor({ collection: null, template });
    await buildAList();

    await userEvent.click(screen.getByRole("button", { name: "Add row" }));
    expect(await screen.findByText("A row with that name already exists.")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Add row" }));

    await waitFor(() => expect(api.createCollection).toHaveBeenCalledTimes(2));
    expect(api.createTheme).toHaveBeenCalledTimes(1);
    expect(api.updateTheme).toHaveBeenCalledWith(55, expect.objectContaining({ tokens: 0 }));
    expect((api.createCollection.mock.calls[1]?.[0] as Record<string, unknown>).theme_id).toBe(55);
  });

  it("does not save the row when its list could not be saved", async () => {
    api.createTheme.mockRejectedValue(new ApiError(422, "Add at least one tag, genre or title."));
    renderEditor({ collection: null, template });
    await buildAList();

    await userEvent.click(screen.getByRole("button", { name: "Add row" }));

    expect(await screen.findByText("Add at least one tag, genre or title.")).toBeInTheDocument();
    expect(api.createCollection).not.toHaveBeenCalled();
  });
});

describe("a saved AI row", () => {
  const refined = {
    ...PREVIEW,
    stats: { named: 50, resolved: 35, in_library: 30, after_rules: 20, unwatched_median: null },
    diff: {
      rules_changed: false,
      added: ["Hereditary"],
      removed: [],
      unchanged: ["Se7en"],
      added_count: 1,
      removed_count: 0,
      tags_added: [],
      tags_removed: [],
      genres_added: [],
      genres_removed: [],
      before_count: 1,
      after_count: 2,
    },
    tokens: 150,
  };

  it("keeps a refinement, then saves the theme in place and the row after it", async () => {
    api.previewTheme.mockResolvedValue(refined);
    renderEditor({ collection: aiRow() });
    await userEvent.type(await screen.findByLabelText("Change it"), "less gore");
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));
    await userEvent.click(await screen.findByRole("button", { name: "Keep" }));
    expect(await screen.findByText(/1 unsaved change/)).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(api.updateCollection).toHaveBeenCalled());
    expect(api.updateTheme).toHaveBeenCalledWith(
      5,
      expect.objectContaining({ tokens: 150, collection_id: 9, draft: expect.objectContaining({ origin: "ai" }) }),
    );
    expect(api.updateCollection.mock.calls[0]?.[0]).toBe(9);
    expect((api.updateCollection.mock.calls[0]?.[1] as Record<string, unknown>).theme_id).toBe(5);
  });

  it("saves only the row when its list didn't change, and never asks for the AI", async () => {
    renderEditor({ collection: aiRow() });
    await screen.findByRole("region", { name: /the list/i });

    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(api.updateCollection).toHaveBeenCalled());
    expect(api.updateTheme).not.toHaveBeenCalled();
    expect(api.previewTheme).not.toHaveBeenCalled();
  });

  it("Discard drops a list that wasn't saved", async () => {
    renderEditor({ collection: aiRow() });
    await userEvent.type(await screen.findByLabelText("Describe it"), " again");
    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));
    expect(await screen.findByText("Not saved yet")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Discard" }));

    await waitFor(() => expect(screen.queryByText("Not saved yet")).not.toBeInTheDocument());
  });
});

describe("tokens spent on a list that was discarded", () => {
  it("are still charged by the next theme save", async () => {
    api.previewTheme.mockResolvedValueOnce({ ...PREVIEW, tokens: 100 }).mockResolvedValueOnce({ ...PREVIEW, tokens: 50 });
    renderEditor({ collection: aiRow() });
    await userEvent.type(await screen.findByLabelText("Describe it"), " one");
    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));
    await screen.findByText("Not saved yet");
    await userEvent.click(screen.getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(screen.queryByText("Not saved yet")).not.toBeInTheDocument());

    await userEvent.type(screen.getByLabelText("Describe it"), " two");
    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));
    await screen.findByText("Not saved yet");
    await userEvent.click(screen.getByRole("button", { name: "Save changes" }));

    await waitFor(() => expect(api.updateTheme).toHaveBeenCalled());
    expect(api.updateTheme).toHaveBeenCalledWith(5, expect.objectContaining({ tokens: 150 }));
  });
});

describe("an ordinary row", () => {
  it("has no AI sections and never asks about AI", async () => {
    renderEditor({ collection: aiRow({ theme_id: null, name: "Hidden Gems", name_template: "" }) });

    expect(await screen.findByText(/Row type: Picked for You/)).toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Try it" })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "AI prompts" })).not.toBeInTheDocument();
    expect(api.getThemeCapabilities).not.toHaveBeenCalled();
    expect(screen.getByRole("link", { name: "Requests" })).toBeInTheDocument();
  });
});
