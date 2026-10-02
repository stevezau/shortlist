import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SeasonEditorDialog, type SeasonEditorTarget } from "@/components/rows/seasons/season-editor-dialog";
import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { SeasonInput, SeasonPreviewInput } from "@/lib/types";

import { BUILTINS, THANKSGIVING, THANKSGIVING_US, preview } from "./season-fixtures";

const mocks = vi.hoisted(() => ({
  getSeasons: vi.fn(),
  getSeasonPresets: vi.fn(),
  previewSeason: vi.fn(),
  getTmdbTags: vi.fn(),
  getPlexCollections: vi.fn(),
  searchLibrary: vi.fn(),
  createSeason: vi.fn(),
  updateSeason: vi.fn(),
  deleteSeason: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: mocks };
});

const NO_TAG =
  "TMDB has no tag matching “father's day”. Try a broader word, or add a collection or your own picks below.";
const MISSING_COLLECTION =
  "Not in your library right now. Kometa only creates its seasonal collections in season; on nights it's missing, this season uses its other sources.";

function renderEditor(
  target: SeasonEditorTarget,
  { rowSize = 15, perPerson = true, tickedHere = ["halloween"] as string[] } = {},
) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const onDeleted = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <SeasonEditorDialog
        target={target}
        rowSize={rowSize}
        perPerson={perPerson}
        rowId={null}
        tickedHere={tickedHere}
        onClose={onClose}
        onSaved={onSaved}
        onDeleted={onDeleted}
      />
    </QueryClientProvider>,
  );
  return { onClose, onSaved, onDeleted };
}

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
  // Built-ins only: a season of the owner's called Thanksgiving would clash with every draft here.
  mocks.getSeasons.mockResolvedValue(BUILTINS);
  mocks.previewSeason.mockResolvedValue(preview());
  mocks.getTmdbTags.mockResolvedValue([]);
  mocks.getPlexCollections.mockResolvedValue([]);
  mocks.searchLibrary.mockResolvedValue([]);
});

describe("SeasonEditorDialog", () => {
  it("says TMDB has no such tag, and what to do instead, when a search finds none", async () => {
    renderEditor({ kind: "create" });
    await userEvent.type(screen.getByLabelText("Search TMDB tags"), "father's day");
    expect(await screen.findByText(NO_TAG)).toBeInTheDocument();
    expect(mocks.getTmdbTags).toHaveBeenLastCalledWith("father's day");
  });

  it("warns that a collection missing from the library tonight adds nothing until it's back", async () => {
    mocks.getPlexCollections.mockResolvedValue([
      { section_key: "1", section_title: "Movies", title: "Thanksgiving Movies", count: 18, smart: false },
    ]);
    mocks.previewSeason.mockImplementation((body: SeasonPreviewInput) =>
      Promise.resolve(
        preview({
          per_collection: (body.collections ?? []).map((c) => ({
            title: c.title,
            section_key: c.section_key,
            found: false,
            in_library: 0,
          })),
        }),
      ),
    );
    renderEditor({ kind: "create" });
    await userEvent.type(screen.getByLabelText("Search your Plex collections"), "thanks");
    await userEvent.click(await screen.findByRole("button", { name: "Add Thanksgiving Movies (Movies)" }));

    expect(await screen.findByText(MISSING_COLLECTION)).toBeInTheDocument();
  });

  it("won't save a season with no films until one is added, then saves it and hands back its slug", async () => {
    mocks.searchLibrary.mockResolvedValue([{ tmdb_id: 11, media_type: "movie", title: "Free Birds", year: 2013 }]);
    mocks.createSeason.mockResolvedValue({ ...THANKSGIVING, slug: "thanksgiving" });
    const { onSaved } = renderEditor({ kind: "create" });

    await userEvent.type(screen.getByLabelText("Season name"), "Thanksgiving");
    await userEvent.click(screen.getByRole("button", { name: "A weekday in a month" }));
    await userEvent.selectOptions(screen.getByLabelText("Which"), "4");
    await userEvent.selectOptions(screen.getByLabelText("Weekday"), "3");
    await userEvent.selectOptions(screen.getByLabelText("Month"), "11");

    const save = screen.getByRole("button", { name: "Save and add to this row" });
    expect(save).toBeDisabled();
    expect(screen.getByText("Add at least one tag, collection or film.")).toBeInTheDocument();

    await userEvent.type(screen.getByLabelText("Search your libraries"), "Free");
    await userEvent.click(await screen.findByRole("button", { name: "Add Free Birds (2013)" }));
    expect(screen.queryByText("Add at least one tag, collection or film.")).toBeNull();
    expect(save).toBeEnabled();

    await userEvent.click(save);
    await waitFor(() => expect(mocks.createSeason).toHaveBeenCalledTimes(1));
    const body = mocks.createSeason.mock.calls[0]?.[0] as SeasonInput;
    expect(body.name).toBe("Thanksgiving");
    expect(body.picks?.[0]?.tmdb_id).toBe(11);
    expect(body.rule).toEqual(expect.objectContaining({ kind: "nth", month: 11, nth: 4, weekday: 3 }));
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith("thanksgiving"));
  });

  it("says a per-person row will look alike when the season has fewer than 100 films", async () => {
    mocks.previewSeason.mockResolvedValue(preview({ total: 26, from_tags: 26, per_tag: { "4543": 26 } }));
    renderEditor({ kind: "preset", preset: THANKSGIVING_US }, { rowSize: 15, perPerson: true });
    expect(
      await screen.findByText("People's rows will be much alike — works best in a shared row"),
    ).toBeInTheDocument();
    expect(screen.getByText("26 in your libraries")).toBeInTheDocument();
  });

  it("opens a ready-made season filled in, and saves where it came from", async () => {
    mocks.createSeason.mockResolvedValue(THANKSGIVING);
    const { onSaved } = renderEditor({ kind: "preset", preset: THANKSGIVING_US });

    expect(screen.getByRole("heading", { name: "Add Thanksgiving (US)" })).toBeInTheDocument();
    expect(screen.getByLabelText("Season name")).toHaveValue("Thanksgiving");
    expect(screen.getByLabelText("Emoji")).toHaveValue("🦃");
    expect(screen.getByRole("button", { name: "A weekday in a month" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByLabelText("Which")).toHaveValue("4");
    expect(screen.getByLabelText("Weekday")).toHaveValue("3");
    expect(screen.getByLabelText("Month")).toHaveValue("11");
    expect(within(screen.getByRole("list", { name: "Chosen tags" })).getByText("thanksgiving")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Save and add to this row" }));
    await waitFor(() => expect(mocks.createSeason).toHaveBeenCalledTimes(1));
    const body = mocks.createSeason.mock.calls[0]?.[0] as SeasonInput & Record<string, unknown>;
    expect(body.preset).toBe("thanksgiving_us");
    expect(body.lead_days).toBe(14);
    expect(body.tags).toEqual([{ id: 4543, name: "thanksgiving" }]);
    // The server refuses any field a season doesn't have.
    expect(body).not.toHaveProperty("key");
    expect(body).not.toHaveProperty("label");
    expect(body).not.toHaveProperty("note");
    await waitFor(() => expect(onSaved).toHaveBeenCalledWith("thanksgiving"));
  });

  it("names the rows a delete takes the season out of, and shows why the server refused it", async () => {
    const refusal =
      "“Thanksgiving” is the only season in “Family picks”. Give those rows another season, or delete them, first.";
    mocks.deleteSeason.mockRejectedValue(new ApiError(409, refusal));
    const { onDeleted } = renderEditor({
      kind: "edit",
      season: { ...THANKSGIVING, used_by: [{ id: 7, name: "Family picks" }] },
    });

    await userEvent.click(screen.getByRole("button", { name: "Delete season" }));
    const confirm = await screen.findByRole("dialog", {
      name: "Remove “Thanksgiving” from Family picks and delete it?",
    });
    await userEvent.click(within(confirm).getByRole("button", { name: "Delete season" }));

    expect(await within(confirm).findByText(refusal)).toBeInTheDocument();
    expect(mocks.deleteSeason).toHaveBeenCalledWith("thanksgiving");
    expect(onDeleted).not.toHaveBeenCalled();
  });

  it("asks plainly when no row uses the season", async () => {
    mocks.deleteSeason.mockResolvedValue(undefined);
    const { onDeleted } = renderEditor({ kind: "edit", season: THANKSGIVING });

    await userEvent.click(screen.getByRole("button", { name: "Delete season" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete “Thanksgiving”?" });
    await userEvent.click(within(confirm).getByRole("button", { name: "Delete season" }));

    await waitFor(() => expect(onDeleted).toHaveBeenCalledWith("thanksgiving"));
  });

  it("shows what is wrong with the date under the date pickers", async () => {
    const problem = "29 February isn't every year — pick 28 February or 1 March.";
    mocks.previewSeason.mockResolvedValue(preview({ next_date: null, rule_error: problem }));
    renderEditor({ kind: "create" });

    expect(await screen.findByText(problem)).toBeInTheDocument();
    expect(screen.getByLabelText("Day")).toHaveAccessibleDescription(problem);
  });

  it("says the first count takes a few seconds while it runs", async () => {
    mocks.previewSeason.mockReturnValue(new Promise(() => {}));
    renderEditor({ kind: "preset", preset: THANKSGIVING_US });
    expect(
      await screen.findByText("Counting films in your libraries… the first count takes a few seconds."),
    ).toBeInTheDocument();
  });

  it("offers Retry when the count fails, with the server's reason", async () => {
    mocks.previewSeason.mockRejectedValueOnce(new ApiError(503, "Add a TMDB API key in Settings first."));
    renderEditor({ kind: "preset", preset: THANKSGIVING_US });
    expect(await screen.findByText("Add a TMDB API key in Settings first.")).toBeInTheDocument();

    mocks.previewSeason.mockResolvedValue(preview({ total: 150, from_tags: 150 }));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Enough for this row")).toBeInTheDocument();
  });

  it("refuses a name another season already has, before saving", async () => {
    renderEditor({ kind: "create" });
    await userEvent.type(screen.getByLabelText("Season name"), "christmas");
    await waitFor(() =>
      expect(screen.getByLabelText("Season name")).toHaveAccessibleDescription(
        /There's already a season called “Christmas”\./,
      ),
    );
    expect(screen.getByRole("button", { name: "Save and add to this row" })).toBeDisabled();
  });

  it("shows the server's duplicate-name refusal by the name", async () => {
    const clash = "There's already a season called “Thanksgiving”.";
    mocks.createSeason.mockRejectedValue(new ApiError(422, clash));
    renderEditor({ kind: "preset", preset: THANKSGIVING_US });

    await userEvent.click(screen.getByRole("button", { name: "Save and add to this row" }));
    await waitFor(() => expect(screen.getByLabelText("Season name")).toHaveAccessibleDescription(new RegExp(clash)));
  });
});
