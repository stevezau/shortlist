import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AiRowSection } from "@/components/rows/ai-row-section";
import { ApiError } from "@/lib/api";
import type * as ApiModule from "@/lib/api";
import { blankInput } from "@/lib/collections";
import type { PendingTheme } from "@/lib/themes";
import type { Collection, CollectionInput, Theme, ThemePreview } from "@/lib/types";

const api = vi.hoisted(() => ({
  getThemeCapabilities: vi.fn(),
  getTheme: vi.fn(),
  getThemePrompts: vi.fn(),
  previewTheme: vi.fn(),
  setAiPause: vi.fn(),
  getTmdbTags: vi.fn(),
  searchLibrary: vi.fn(),
  listCollections: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, ...api } };
});

function theme(patch: Partial<Theme> = {}): Theme {
  return {
    id: 5,
    slug: "twist-endings",
    name: "Twist endings",
    emoji: "🌀",
    brief: "films with a twist ending",
    origin: "ai",
    media: ["movie"],
    tags: [{ id: 111, name: "twist ending" }],
    genres: ["thriller"],
    excluded_genres: [],
    collections: [],
    picks: [
      { tmdb_id: 1, media: "movie", origin: "ai", reason: "The twist lands in the last minute", title: "Se7en", year: 1995 },
      { tmdb_id: 2, media: "movie", origin: "owner", reason: null, title: "The Prestige", year: 2006 },
    ],
    rules: { max_runtime: 140 },
    content_hash: "h1",
    ai_tokens: 400,
    stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25 },
    ...patch,
  };
}

function preview(patch: Partial<ThemePreview> = {}): ThemePreview {
  return {
    draft: theme({ id: null, ai_tokens: 0, stats: {} }),
    stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25, unwatched_median: null, truncated: false, runtime_total: 0, runtime_checked: 0, ai_kept: 12 },
    diff: null,
    tokens: 321,
    ...patch,
  };
}

function savedRow(patch: Partial<Collection> = {}): Collection {
  return { id: 9, theme_id: 5, ai_paused: false, ai_tokens: 1234, ...patch } as unknown as Collection;
}

type Props = {
  collection?: Collection | null;
  input?: CollectionInput;
  pending?: PendingTheme | null;
  tokensSpent?: number;
};

const spent = vi.fn();
const changed = vi.fn();

function Harness({ collection = null, input = blankInput(), pending = null, tokensSpent = 0 }: Props) {
  const [current, setCurrent] = useState<PendingTheme | null>(pending);
  return (
    <AiRowSection
      input={input}
      collection={collection}
      pending={current}
      tokensSpent={tokensSpent}
      onPending={(next) => {
        changed(next);
        setCurrent(next);
      }}
      onSpent={spent}
    />
  );
}

function renderSection(props: Props = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </MemoryRouter>
    );
  }
  return render(<Harness {...props} />, { wrapper: Wrapper });
}

beforeEach(() => {
  for (const fn of Object.values(api)) fn.mockReset();
  spent.mockReset();
  changed.mockReset();
  api.getThemeCapabilities.mockResolvedValue({ ai: true });
  api.getTheme.mockResolvedValue(theme());
  api.getThemePrompts.mockResolvedValue({ guidance: "You curate themed rows.", mechanics: "Reply with JSON." });
  api.previewTheme.mockResolvedValue(preview());
  api.getTmdbTags.mockResolvedValue([]);
  api.searchLibrary.mockResolvedValue([]);
});

describe("AiRowSection loading and failing", () => {
  it("shows a skeleton while it asks whether an AI provider is set", () => {
    api.getThemeCapabilities.mockReturnValue(new Promise(() => undefined));
    renderSection();

    expect(screen.getByRole("status", { name: /loading/i })).toBeInTheDocument();
  });

  it("says what went wrong and offers a retry that works", async () => {
    api.getThemeCapabilities.mockRejectedValueOnce(new ApiError(503, "Shortlist didn't answer."));
    renderSection();

    expect(await screen.findByRole("alert")).toHaveTextContent("Shortlist didn't answer.");
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));

    expect(await screen.findByLabelText("Describe it")).toBeInTheDocument();
  });

  it("shows a skeleton, then an error with a retry, while a saved row's list loads", async () => {
    api.getTheme.mockRejectedValueOnce(new ApiError(404, "theme not found"));
    renderSection({ collection: savedRow() });

    expect(await screen.findByText("theme not found")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));

    expect(await screen.findByText("Twist endings")).toBeInTheDocument();
  });
});

describe("AiRowSection with no list yet", () => {
  it("explains what to do, and keeps Build the list off until something is described", async () => {
    renderSection();

    expect(await screen.findByText(/no list yet/i)).toHaveTextContent(/describe the row/i);
    const build = screen.getByRole("button", { name: "Build the list" });
    expect(build).toBeDisabled();

    await userEvent.type(screen.getByLabelText("Describe it"), "films with a twist");

    expect(build).toBeEnabled();
  });

  it("starts a new row's note with the fact that it is off until switched on", async () => {
    renderSection();

    expect(await screen.findByText(/starts switched off/i)).toBeInTheDocument();
  });
});

describe("AiRowSection building", () => {
  it("builds one list from the description and shows what it found", async () => {
    renderSection({ collection: savedRow({ theme_id: null }), input: { ...blankInput(), media: "movie" } });
    await userEvent.type(await screen.findByLabelText("Describe it"), "  films with a twist ending ");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    await waitFor(() => expect(screen.getByRole("region", { name: /the list/i })).toBeInTheDocument());
    expect(api.previewTheme).toHaveBeenCalledWith({
      brief: "films with a twist ending",
      media: "movie",
      collection_id: 9,
      guidance: "",
    });
    const card = within(screen.getByRole("region", { name: /the list/i }));
    expect(card.getByText("60")).toBeInTheDocument(); // named
    expect(card.getByText("25")).toBeInTheDocument(); // after limits
    expect(card.getByText("Se7en (1995)")).toBeInTheDocument();
    expect(card.getByText("The Prestige (2006)")).toBeInTheDocument();
    expect(card.getByText("AI")).toBeInTheDocument();
    expect(card.getByText("You")).toBeInTheDocument();
    // Was "10 titles the AI named aren't on your server" (resolved minus in_library, which counts tag matches too).
    expect(card.getByText("12 of the AI’s 60 titles are on your server.")).toBeInTheDocument();
    expect(screen.getByText(/uses your AI provider once per theme/i)).toBeInTheDocument();
    expect(spent).toHaveBeenCalledWith(321);
    expect(changed).toHaveBeenCalledWith(expect.objectContaining({ origin: "ai", draft: expect.objectContaining({ name: "Twist endings" }) }));
  });

  it("says the AI's list was cut short, and how many titles were kept, only when it was", async () => {
    api.previewTheme.mockResolvedValueOnce(
      preview({ stats: { named: 31, resolved: 20, in_library: 15, after_rules: 12, unwatched_median: null, truncated: true, runtime_total: 0, runtime_checked: 0, ai_kept: 12 } }),
    );
    renderSection({ collection: savedRow({ theme_id: null }), input: { ...blankInput(), media: "movie" } });
    await userEvent.type(await screen.findByLabelText("Describe it"), "films with a twist ending");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.getByText(/the AI.s list was cut short; 31 titles kept/i)).toBeInTheDocument();
  });

  it("says when a theme was topped up and by how many titles, and says nothing for one that never was", async () => {
    api.getTheme.mockResolvedValue(
      theme({ topped_up_at: "2026-10-10T01:30:00Z", stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25, topped_up: 12 } }),
    );
    renderSection({ collection: savedRow() });

    expect(await screen.findByText(/topped up once on .*2026 — 12 more titles/i)).toBeInTheDocument();
  });

  it("shows no top-up line for a theme that was never topped up", async () => {
    renderSection({ collection: savedRow() });

    await screen.findByRole("region", { name: /the list/i });
    expect(screen.queryByText(/topped up once/i)).not.toBeInTheDocument();
  });

  it("says a row covering both kinds stays out of the TV libraries when the AI named only films", async () => {
    api.previewTheme.mockResolvedValueOnce(preview());
    renderSection({ collection: savedRow({ theme_id: null }), input: { ...blankInput(), media: "both" } });
    await userEvent.type(await screen.findByLabelText("Describe it"), "films with a twist ending");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.getByText(/films only — the AI named no TV series/i)).toBeInTheDocument();
  });

  it("says nothing about kinds when the row covers one kind or the AI named both", async () => {
    api.previewTheme.mockResolvedValueOnce(preview());
    renderSection({ collection: savedRow({ theme_id: null }), input: { ...blankInput(), media: "movie" } });
    await userEvent.type(await screen.findByLabelText("Describe it"), "films with a twist ending");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.queryByText(/only — the AI named no/i)).not.toBeInTheDocument();
  });

  it("says how many running times a preview checked, only when it checked fewer than all", async () => {
    api.previewTheme.mockResolvedValueOnce(
      preview({
        stats: { named: 31, resolved: 20, in_library: 15, after_rules: 12, unwatched_median: null, truncated: false, runtime_total: 900, runtime_checked: 400, ai_kept: 6 },
      }),
    );
    renderSection({ collection: savedRow({ theme_id: null }), input: { ...blankInput(), media: "movie" } });
    await userEvent.type(await screen.findByLabelText("Describe it"), "films with a twist ending");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.getByText("Checked running time for 400 of 900 titles; the nightly run checks the rest.")).toBeInTheDocument();
  });

  it("shows no cut-short note for a whole list", async () => {
    renderSection({ collection: savedRow({ theme_id: null }), input: { ...blankInput(), media: "movie" } });
    await userEvent.type(await screen.findByLabelText("Describe it"), "films with a twist ending");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.queryByText(/cut short/i)).not.toBeInTheDocument();
  });

  it("does not count tokens twice when the same list is built again", async () => {
    renderSection();
    await userEvent.type(await screen.findByLabelText("Describe it"), "twists");
    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));
    await screen.findByRole("region", { name: /the list/i });

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    await waitFor(() => expect(api.previewTheme).toHaveBeenCalledTimes(1));
    expect(spent).toHaveBeenCalledTimes(1);
  });

  it("sends the owner's own guidance with the build", async () => {
    renderSection({ input: { ...blankInput(), ai_instructions: { mode: "own", text: "Only films before 2000." } } });
    await userEvent.type(await screen.findByLabelText("Describe it"), "twists");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    await waitFor(() =>
      expect(api.previewTheme).toHaveBeenCalledWith(expect.objectContaining({ guidance: "Only films before 2000." })),
    );
  });

  it("says why a build failed, in the server's words, and tries again on request", async () => {
    api.previewTheme.mockRejectedValueOnce(new ApiError(422, "The AI didn't suggest anything Shortlist could find."));
    renderSection();
    await userEvent.type(await screen.findByLabelText("Describe it"), "twists");
    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("The AI didn't suggest anything Shortlist could find.");
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));

    expect(await screen.findByRole("region", { name: /the list/i })).toBeInTheDocument();
  });

  it("shows the saved list of a saved row, with its saved counts", async () => {
    renderSection({ collection: savedRow() });

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.getByText("Twist endings")).toBeInTheDocument();
    expect(card.getByText("Se7en (1995)")).toBeInTheDocument();
    expect(card.getByText("40")).toBeInTheDocument();
    expect(screen.getByLabelText("Describe it")).toHaveValue("films with a twist ending");
  });
});

describe("AiRowSection changing a list", () => {
  const refined = preview({
    draft: theme({
      id: null,
      picks: [
        { tmdb_id: 2, media: "movie", origin: "owner", reason: null, title: "The Prestige", year: 2006 },
        { tmdb_id: 3, media: "movie", origin: "ai", reason: "Quiet horror", title: "Hereditary", year: 2018 },
      ],
    }),
    stats: { named: 55, resolved: 38, in_library: 31, after_rules: 22, unwatched_median: null, truncated: false, runtime_total: 0, runtime_checked: 0, ai_kept: 12 },
    diff: {
      rules_changed: true,
      added: ["Hereditary"],
      removed: ["Se7en"],
      unchanged: ["The Prestige"],
      added_count: 1,
      removed_count: 1,
      tags_added: ["slasher"],
      tags_removed: ["gore"],
      genres_added: ["Horror"],
      genres_removed: ["Comedy"],
      before_count: 2,
      after_count: 3,
    },
    tokens: 150,
  });

  it("refines the saved list by what was typed and shows what would change", async () => {
    api.previewTheme.mockResolvedValue(refined);
    renderSection({ collection: savedRow() });
    await userEvent.type(await screen.findByLabelText("Change it"), "less gore");

    await userEvent.click(screen.getByRole("button", { name: "Change it" }));

    const diff = within(await screen.findByRole("region", { name: /what would change/i }));
    expect(api.previewTheme).toHaveBeenCalledWith(
      expect.objectContaining({ change: "less gore", current_theme_id: 5, collection_id: 9 }),
    );
    // The description stays the row's: only the change is sent, never the typed words as a new brief.
    expect(api.previewTheme.mock.calls[0]![0]).not.toHaveProperty("brief");
    expect(diff.getByText("Hereditary")).toBeInTheDocument();
    expect(diff.getByText("Se7en")).toBeInTheDocument();
    expect(diff.getByText(/limits changed/i)).toBeInTheDocument();
    expect(diff.getByText("slasher")).toBeInTheDocument();
    expect(diff.getByText("gore")).toBeInTheDocument();
    expect(diff.getByText("Horror")).toBeInTheDocument();
    expect(diff.getByText("Comedy")).toBeInTheDocument();
    expect(diff.getByText(/2 before, 3 after/i)).toBeInTheDocument();
    expect(diff.getByText(/1 stays/i)).toBeInTheDocument();
    expect(diff.getByText(/22 titles/i)).toBeInTheDocument();
    expect(spent).toHaveBeenCalledWith(150);
  });

  it("Keep puts the new list in the editor, to be saved with the row", async () => {
    api.previewTheme.mockResolvedValue(refined);
    renderSection({ collection: savedRow() });
    await userEvent.type(await screen.findByLabelText("Change it"), "less gore");
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));
    await screen.findByRole("region", { name: /what would change/i });

    await userEvent.click(screen.getByRole("button", { name: "Keep" }));

    expect(changed).toHaveBeenCalledWith(
      expect.objectContaining({ origin: "ai", draft: expect.objectContaining({ picks: refined.draft.picks }) }),
    );
    expect(screen.queryByRole("region", { name: /what would change/i })).not.toBeInTheDocument();
  });

  it("Discard leaves the list as it was", async () => {
    api.previewTheme.mockResolvedValue(refined);
    renderSection({ collection: savedRow() });
    await userEvent.type(await screen.findByLabelText("Change it"), "less gore");
    await userEvent.click(screen.getByRole("button", { name: "Change it" }));
    await screen.findByRole("region", { name: /what would change/i });

    await userEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(changed).not.toHaveBeenCalled();
    expect(screen.queryByRole("region", { name: /what would change/i })).not.toBeInTheDocument();
  });

  it("shows no Change it box for a row that is not saved yet", async () => {
    renderSection({ collection: null });

    await screen.findByLabelText("Describe it");
    expect(screen.queryByLabelText("Change it")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Change it" })).not.toBeInTheDocument();
    expect(screen.queryByText(/save the row first/i)).not.toBeInTheDocument();
  });

  it("shows no Change it box for a saved row that has no list yet", async () => {
    renderSection({ collection: savedRow({ theme_id: null }) });

    await screen.findByLabelText("Describe it");
    expect(screen.queryByLabelText("Change it")).not.toBeInTheDocument();
  });

  it("shows no Change it box while a list built here is waiting to be saved", async () => {
    renderSection({ collection: savedRow(), pending: { draft: theme({ id: null }), stats: null, origin: "ai" } });

    await screen.findByLabelText("Describe it");
    expect(screen.queryByLabelText("Change it")).not.toBeInTheDocument();
  });

  it("shows the Change it box once the row is saved with a list", async () => {
    renderSection({ collection: savedRow() });

    expect(await screen.findByLabelText("Change it")).toBeEnabled();
  });
});

describe("AiRowSection without an AI provider", () => {
  beforeEach(() => api.getThemeCapabilities.mockResolvedValue({ ai: false }));

  it("hides the AI half and offers the list's tags, genres, limits and titles to edit by hand", async () => {
    renderSection();

    expect(await screen.findByLabelText("Search TMDB tags")).toBeInTheDocument();
    expect(screen.queryByLabelText("Describe it")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Build the list" })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Add a genre")).toBeInTheDocument();
    expect(screen.getByLabelText("Longest it can run (minutes)")).toBeInTheDocument();
    expect(screen.getByLabelText("Search your libraries")).toBeInTheDocument();
    expect(screen.getByText(/no AI provider is set/i)).toBeInTheDocument();
  });

  it("a hand edit becomes the editor's pending list, written by hand and costing nothing", async () => {
    renderSection({ input: { ...blankInput(), media: "movie" } });

    await userEvent.type(await screen.findByLabelText("Theme name"), "Heists");
    await userEvent.selectOptions(screen.getByLabelText("Add a genre"), "Crime");

    const last = changed.mock.calls.at(-1)?.[0] as PendingTheme;
    expect(last.origin).toBe("manual");
    expect(last.stats).toBeNull();
    expect(last.draft.name).toBe("Heists");
    expect(last.draft.genres).toEqual(["crime"]);
    expect(last.draft.media).toEqual(["movie"]);
    expect(api.previewTheme).not.toHaveBeenCalled();
  });

  it("loads a saved row's list into the fields", async () => {
    renderSection({ collection: savedRow() });

    expect(await screen.findByLabelText("Theme name")).toHaveValue("Twist endings");
    expect(screen.getByLabelText("Longest it can run (minutes)")).toHaveValue(140);
    expect(screen.getByText("twist ending")).toBeInTheDocument();
  });
});

describe("AiRowSection pausing", () => {
  it("disables Build the list and Change it, and says why, while the row's AI is paused", async () => {
    renderSection({ collection: savedRow({ ai_paused: true }) });
    await userEvent.type(await screen.findByLabelText("Describe it"), "twists");
    await userEvent.type(screen.getByLabelText("Change it"), "less gore");

    expect(screen.getByRole("button", { name: "Build the list" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Change it" })).toBeDisabled();
    expect(screen.getAllByText(/AI is paused for this row/i).length).toBeGreaterThan(0);
    expect(screen.getByRole("switch", { name: "Pause AI for this row" })).toBeChecked();
  });

  it("pauses a saved row at once, apart from Save changes", async () => {
    api.setAiPause.mockResolvedValue(savedRow({ ai_paused: true }));
    renderSection({ collection: savedRow() });

    await userEvent.click(await screen.findByRole("switch", { name: "Pause AI for this row" }));

    await waitFor(() => expect(api.setAiPause).toHaveBeenCalledWith(9, true));
  });

  it("says when a pause could not be saved", async () => {
    api.setAiPause.mockRejectedValue(new ApiError(422, "Only an AI row has AI to pause."));
    renderSection({ collection: savedRow() });

    await userEvent.click(await screen.findByRole("switch", { name: "Pause AI for this row" }));

    expect(await screen.findByText("Only an AI row has AI to pause.")).toBeInTheDocument();
  });

  it("offers no pause for a row that is not saved yet", async () => {
    renderSection({ collection: null });

    await screen.findByLabelText("Describe it");
    expect(screen.queryByRole("switch", { name: "Pause AI for this row" })).not.toBeInTheDocument();
  });
});

describe("AiRowSection usage", () => {
  it("says how many tokens the row has used, and what this edit has spent that is not saved yet", async () => {
    renderSection({ collection: savedRow(), tokensSpent: 500 });

    expect(await screen.findByText(/used 1,234 tokens on this row/i)).toHaveTextContent(/500 more.*not saved/i);
  });

  it("says none yet for a row that has used none", async () => {
    renderSection({ collection: savedRow({ ai_tokens: 0 }) });

    expect(await screen.findByText(/used 0 tokens on this row/i)).toBeInTheDocument();
  });
});

describe("AiRowSection over time", () => {
  beforeEach(() => {
    api.listCollections.mockResolvedValue([]);
  });

  it("shows Explore and the over-time controls for a row with a theme", async () => {
    renderSection({ collection: savedRow({ slug: "mine", theme_mode: "fixed" } as Partial<Collection>) });

    expect(await screen.findByLabelText("How much changes each time")).toBeInTheDocument();
    expect(screen.getByRole("radio", { name: "Keep the same theme" })).toBeChecked();
  });

  it("shows them for a new row once its list is built", () => {
    renderSection({ pending: { draft: theme({ id: null }), stats: null, origin: "ai" } });

    expect(screen.getByLabelText("How much changes each time")).toBeInTheDocument();
  });

  it("shows neither for a row with no theme", async () => {
    renderSection({ collection: savedRow({ theme_id: null }) });
    await screen.findByText(/No list yet/);

    expect(screen.queryByLabelText("How much changes each time")).toBeNull();
    expect(screen.queryByRole("radio", { name: "Keep the same theme" })).toBeNull();
  });
});

describe("AiRowSection counts", () => {
  it("says how many of the AI's own titles are on the server, apart from the tag and genre matches", async () => {
    renderSection({ collection: savedRow({ theme_id: null }), input: { ...blankInput(), media: "movie" } });
    await userEvent.type(await screen.findByLabelText("Describe it"), "films with a twist ending");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.getByText("12 of the AI’s 60 titles are on your server.")).toBeInTheDocument();
    expect(card.getByText("Plus 13 more that match its tags and genres.")).toBeInTheDocument();
    // `in_library` (30) is the AI's picks AND every tag match; it must not be shown as what the AI found.
    expect(card.queryByText("On your server")).not.toBeInTheDocument();
    expect(card.queryByText("30")).not.toBeInTheDocument();
  });

  it("leaves out the tag line when every title in the row is the AI's", async () => {
    api.previewTheme.mockResolvedValueOnce(
      preview({ stats: { named: 5, resolved: 5, in_library: 4, after_rules: 4, unwatched_median: null, truncated: false, runtime_total: 0, runtime_checked: 0, ai_kept: 4 } }),
    );
    renderSection();
    await userEvent.type(await screen.findByLabelText("Describe it"), "twists");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.getByText("4 of the AI’s 5 titles are on your server.")).toBeInTheDocument();
    expect(card.queryByText(/more that match/i)).not.toBeInTheDocument();
  });

  it("shows a list saved before the AI's own count existed without inventing one", async () => {
    renderSection({ collection: savedRow() });

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.getByText("Named by the AI")).toBeInTheDocument();
    expect(card.queryByText(/of the AI.s \d+ titles/i)).not.toBeInTheDocument();
    expect(card.queryByText("On your server")).not.toBeInTheDocument();
  });

  it("shows a saved list's own count", async () => {
    api.getTheme.mockResolvedValue(
      theme({ stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25, ai_kept: 9 } }),
    );
    renderSection({ collection: savedRow() });

    const card = within(await screen.findByRole("region", { name: /the list/i }));
    expect(card.getByText("9 of the AI’s 60 titles are on your server.")).toBeInTheDocument();
    expect(card.getByText("Plus 16 more that match its tags and genres.")).toBeInTheDocument();
  });
});

describe("AiRowSection limits", () => {
  const limited = () =>
    theme({ rules: { max_runtime: 140, min_rating: 7, min_year: 1990, max_year: 2010, min_votes: 500 } });

  it("clears a limit with its x, and the list saved has no such limit", async () => {
    api.getTheme.mockResolvedValue(limited());
    renderSection({ collection: savedRow() });
    const limits = within(await screen.findByRole("list", { name: "Limits" }));

    await userEvent.click(limits.getByRole("button", { name: "Remove limit: Rating 7+" }));

    const pending = changed.mock.calls.at(-1)?.[0] as PendingTheme;
    expect(pending.draft.rules).toEqual({ max_runtime: 140, min_year: 1990, max_year: 2010, min_votes: 500 });
    expect(pending.origin).toBe("ai");
    expect(limits.queryByText("Rating 7+")).not.toBeInTheDocument();
    expect(limits.getByText("Up to 140 min")).toBeInTheDocument();
  });

  it("clears both ends of the years with one x", async () => {
    api.getTheme.mockResolvedValue(limited());
    renderSection({ collection: savedRow() });
    const limits = within(await screen.findByRole("list", { name: "Limits" }));

    await userEvent.click(limits.getByRole("button", { name: "Remove limit: Released 1990–2010" }));

    const pending = changed.mock.calls.at(-1)?.[0] as PendingTheme;
    expect(pending.draft.rules).toEqual({ max_runtime: 140, min_rating: 7, min_votes: 500 });
  });

  it("clears the running time and the votes floor", async () => {
    api.getTheme.mockResolvedValue(limited());
    renderSection({ collection: savedRow() });
    const limits = within(await screen.findByRole("list", { name: "Limits" }));

    await userEvent.click(limits.getByRole("button", { name: "Remove limit: Up to 140 min" }));
    await userEvent.click(limits.getByRole("button", { name: "Remove limit: At least 500 votes" }));

    const pending = changed.mock.calls.at(-1)?.[0] as PendingTheme;
    expect(pending.draft.rules).toEqual({ min_rating: 7, min_year: 1990, max_year: 2010 });
  });

  it("keeps a hand-written list marked as written by hand when a limit is cleared", async () => {
    api.getTheme.mockResolvedValue(theme({ origin: "manual", rules: { min_rating: 7 } }));
    renderSection({ collection: savedRow() });

    await userEvent.click(await screen.findByRole("button", { name: "Remove limit: Rating 7+" }));

    expect((changed.mock.calls.at(-1)?.[0] as PendingTheme).origin).toBe("manual");
  });
});

describe("AiRowSection while it builds", () => {
  it("says it is working and may take a couple of minutes, and disables Build the list", async () => {
    let finish: (value: ThemePreview) => void = () => undefined;
    api.previewTheme.mockReturnValue(new Promise<ThemePreview>((resolve) => (finish = resolve)));
    renderSection();
    await userEvent.type(await screen.findByLabelText("Describe it"), "twists");

    await userEvent.click(screen.getByRole("button", { name: "Build the list" }));

    const status = await screen.findByRole("status", { name: /building the list/i });
    expect(status).toHaveTextContent(/building the list/i);
    expect(status).toHaveTextContent(/couple of minutes/i);
    expect(screen.getByRole("button", { name: "Build the list" })).toBeDisabled();

    finish(preview());
    await screen.findByRole("region", { name: /the list/i });
    expect(screen.queryByRole("status", { name: /building the list/i })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Build the list" })).toBeEnabled();
  });

  it("says it is changing the list while a change is asked for", async () => {
    api.previewTheme.mockReturnValue(new Promise<ThemePreview>(() => undefined));
    renderSection({ collection: savedRow() });
    await userEvent.type(await screen.findByLabelText("Change it"), "less gore");

    await userEvent.click(screen.getByRole("button", { name: "Change it" }));

    expect(await screen.findByRole("status", { name: /changing the list/i })).toHaveTextContent(/couple of minutes/i);
    expect(screen.getByRole("button", { name: "Change it" })).toBeDisabled();
  });
});
