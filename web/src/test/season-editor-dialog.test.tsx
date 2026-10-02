import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { SeasonEditorDialog, type SeasonEditorTarget } from "@/components/rows/seasons/season-editor-dialog";
import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { SeasonRow } from "@/lib/season-verdict";
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
  getSeasonNextDate: vi.fn(),
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
  {
    row = { size: 15, perPerson: true, media: "movie", libraryKeys: [] } as SeasonRow,
    tickedHere = ["halloween"] as string[],
    savedRow = null as { id: number; seasons: string[] } | null,
  } = {},
) {
  const onClose = vi.fn();
  const onSaved = vi.fn();
  const onDeleted = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <SeasonEditorDialog
          target={target}
          row={row}
          savedRow={savedRow}
          tickedHere={tickedHere}
          onClose={onClose}
          onSaved={onSaved}
          onDeleted={onDeleted}
        />
      </QueryClientProvider>
    </MemoryRouter>,
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
  mocks.getSeasonNextDate.mockResolvedValue({ next_date: "2026-11-26", rule_error: null });
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
      {
        section_key: "1",
        section_title: "Movies",
        title: "Thanksgiving Movies",
        count: 18,
        smart: false,
        media_type: "movie",
      },
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
    renderEditor({ kind: "preset", preset: THANKSGIVING_US });
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
    // It no longer asks the question it just failed to carry out, and offers no second try.
    expect(within(confirm).getByRole("heading", { name: "Can't delete “Thanksgiving” yet" })).toBeInTheDocument();
    expect(within(confirm).getByRole("button", { name: "Delete season" })).toBeDisabled();
    expect(within(confirm).queryByText(/keeps its other seasons/)).toBeNull();
  });

  it("says one row keeps its other seasons, and two rows keep theirs", async () => {
    renderEditor({ kind: "edit", season: { ...THANKSGIVING, used_by: [{ id: 7, name: "🦃 Thanksgiving picks" }] } });
    await userEvent.click(screen.getByRole("button", { name: "Delete season" }));
    const one = await screen.findByRole("dialog", { name: "Remove “Thanksgiving” from 🦃 Thanksgiving picks and delete it?" });
    expect(one).toHaveAccessibleDescription(/^That row keeps its other seasons\./);
  });

  it("says those rows keep their other seasons when there are several", async () => {
    renderEditor({
      kind: "edit",
      season: { ...THANKSGIVING, used_by: [{ id: 7, name: "A" }, { id: 8, name: "B" }] },
    });
    await userEvent.click(screen.getByRole("button", { name: "Delete season" }));
    const several = await screen.findByRole("dialog", { name: "Remove “Thanksgiving” from A and B and delete it?" });
    expect(several).toHaveAccessibleDescription(/^Those rows keep their other seasons\./);
  });

  it("gives focus back to Delete season when the confirm is cancelled", async () => {
    renderEditor({ kind: "edit", season: THANKSGIVING });
    const opener = screen.getByRole("button", { name: "Delete season" });
    await userEvent.click(opener);
    const confirm = await screen.findByRole("dialog", { name: "Delete “Thanksgiving”?" });
    await userEvent.click(within(confirm).getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(opener).toHaveFocus());
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
    mocks.getSeasonNextDate.mockResolvedValue({ next_date: null, rule_error: problem });
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

  it("offers Retry when the count fails in a way that may pass next time, with the server's reason", async () => {
    mocks.previewSeason.mockRejectedValueOnce(new ApiError(502, "HTTPError: TMDB timed out"));
    renderEditor({ kind: "preset", preset: THANKSGIVING_US });
    expect(await screen.findByText("HTTPError: TMDB timed out")).toBeInTheDocument();

    mocks.previewSeason.mockResolvedValue(preview({ total: 150, from_tags: 150 }));
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText("Enough films for this row")).toBeInTheDocument();
  });

  it("counts for the row it was opened from: its media and libraries, in that row's word", async () => {
    mocks.previewSeason.mockResolvedValue(
      preview({ total: 72, movies: 56, shows: 16, from_tags: 72, per_tag: { "4543": 72 } }),
    );
    renderEditor(
      { kind: "preset", preset: THANKSGIVING_US },
      { row: { size: 15, perPerson: false, media: "both", libraryKeys: ["1", "2"] } },
    );

    expect(await screen.findByText("titles in your libraries")).toBeInTheDocument();
    expect(screen.getByText("Enough titles for this row")).toBeInTheDocument();
    const body = mocks.previewSeason.mock.calls[0]?.[0] as SeasonPreviewInput;
    expect([body.media, body.library_keys]).toEqual(["both", ["1", "2"]]);
  });

  it("says a row of both is short when one of its libraries' types is, whatever the total", async () => {
    mocks.previewSeason.mockResolvedValue(preview({ total: 40, movies: 40, shows: 0, from_tags: 40 }));
    renderEditor(
      { kind: "preset", preset: THANKSGIVING_US },
      { row: { size: 15, perPerson: false, media: "both", libraryKeys: [] } },
    );

    expect(await screen.findByText("Too few shows to fill this row's TV library (0 of 15)")).toBeInTheDocument();
  });

  it.each([
    ["movie", "Films"],
    ["show", "Shows"],
    ["both", "Titles"],
  ] as const)("heads the sources of a %s row “%s”", async (media, heading) => {
    renderEditor({ kind: "create" }, { row: { size: 15, perPerson: true, media, libraryKeys: [] } });
    expect(await screen.findByRole("heading", { name: heading })).toBeInTheDocument();
  });

  it("says a TV collection holds shows", async () => {
    mocks.getPlexCollections.mockResolvedValue([
      { section_key: "2", section_title: "TV", title: "Thanksgiving TV", count: 4, smart: false, media_type: "show" },
    ]);
    renderEditor({ kind: "create" });
    await userEvent.type(screen.getByLabelText("Search your Plex collections"), "thanks");

    expect(await screen.findByText("TV · 4 shows")).toBeInTheDocument();
  });

  it("sends the owner to Settings, not Retry, when there is no TMDB key", async () => {
    mocks.previewSeason.mockRejectedValue(new ApiError(503, "Add a TMDB API key in Settings first."));
    renderEditor({ kind: "preset", preset: THANKSGIVING_US });
    expect(await screen.findByText("Add a TMDB API key in Settings first.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Open Settings in a new tab" })).toHaveAttribute("href", "/settings#connections");
    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("opens with the cursor in Season name", async () => {
    renderEditor({ kind: "create" });
    await waitFor(() => expect(screen.getByLabelText("Season name")).toHaveFocus());
  });

  it("puts focus back in the search box after an Add, for a film and for a tag", async () => {
    mocks.searchLibrary.mockResolvedValue([{ tmdb_id: 11, media_type: "movie", title: "Free Birds", year: 2013 }]);
    mocks.getTmdbTags.mockResolvedValue([{ id: 4543, name: "thanksgiving", movies: 133 }]);
    renderEditor({ kind: "create" });

    const films = screen.getByLabelText("Search your libraries");
    await userEvent.type(films, "Free");
    await userEvent.click(await screen.findByRole("button", { name: "Add Free Birds (2013)" }));
    expect(films).toHaveFocus();

    const tags = screen.getByLabelText("Search TMDB tags");
    await userEvent.type(tags, "thanks");
    await userEvent.click(await screen.findByRole("button", { name: "Add tag thanksgiving" }));
    expect(tags).toHaveFocus();
    // The next Tab goes on through the results, not back to the top of the dialog.
    await userEvent.tab();
    expect(screen.getByLabelText("Emoji")).not.toHaveFocus();
  });

  it("says beside the emoji box when it is empty", async () => {
    renderEditor({ kind: "create" });
    const emoji = screen.getByLabelText("Emoji");
    await userEvent.clear(emoji);
    expect(emoji).toHaveAttribute("aria-invalid", "true");
    expect(emoji).toHaveAccessibleDescription("Add an emoji.");
  });

  it("won't delete the only season ticked in this row", async () => {
    renderEditor({ kind: "edit", season: THANKSGIVING }, { tickedHere: ["thanksgiving"] });
    await userEvent.click(screen.getByRole("button", { name: "Delete season" }));
    const confirm = await screen.findByRole("dialog", { name: "Can't delete “Thanksgiving” yet" });
    expect(confirm).toHaveAccessibleDescription(/only season ticked in this row/);
    expect(within(confirm).queryByRole("button", { name: "Delete season" })).toBeNull();
    // The footer's Close, beside the corner ×, which Radix also names "Close".
    expect(within(confirm).getAllByRole("button", { name: "Close" })).toHaveLength(2);
  });

  it("asks for the row to be saved first when, as saved, it follows only this season", async () => {
    // The form already ticks Christmas as well, but the server judges the SAVED row: it would refuse.
    renderEditor(
      { kind: "edit", season: { ...THANKSGIVING, used_by: [{ id: 3, name: "Turkey" }] } },
      { tickedHere: ["thanksgiving", "christmas"], savedRow: { id: 3, seasons: ["thanksgiving"] } },
    );
    await userEvent.click(screen.getByRole("button", { name: "Delete season" }));
    const confirm = await screen.findByRole("dialog", { name: "Can't delete “Thanksgiving” yet" });
    expect(confirm).toHaveAccessibleDescription(/^Save this row first, then delete the season\./);
    expect(within(confirm).queryByText(/keep their other seasons|keeps its other seasons/)).toBeNull();
    expect(within(confirm).queryByRole("button", { name: "Delete season" })).toBeNull();
    await userEvent.click(within(confirm).getAllByRole("button", { name: "Close" }).at(-1) as HTMLElement);
    expect(mocks.deleteSeason).not.toHaveBeenCalled();
  });

  it("keeps the next date when the count fails: it comes from the date, not the count", async () => {
    mocks.previewSeason.mockRejectedValue(new ApiError(500, "Internal error"));
    renderEditor({ kind: "preset", preset: THANKSGIVING_US });
    expect(await screen.findByText("Internal error")).toBeInTheDocument();
    expect(await screen.findByText(/^Next: /)).toBeInTheDocument();
    expect(mocks.getSeasonNextDate).toHaveBeenCalledWith(expect.objectContaining({ kind: "nth", month: 11, nth: 4 }));
  });

  it("invites a source, rather than judging a season with none", async () => {
    renderEditor({ kind: "create" });
    expect(
      await screen.findByText("Add a tag, a collection or a film to see what this season finds."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Too few/)).toBeNull();
    expect(mocks.previewSeason).not.toHaveBeenCalled();
  });

  it("keeps the count and verdict in one line by the buttons, for a screen too narrow for the summary", async () => {
    mocks.previewSeason.mockResolvedValue(preview({ total: 26, from_tags: 26 }));
    renderEditor({ kind: "preset", preset: THANKSGIVING_US });
    // Radix portals the dialog to <body>, outside the render container.
    await waitFor(() =>
      expect(document.querySelector("[data-count-line]")).toHaveTextContent("26 films · People's rows will be much alike"),
    );
  });

  it("names the first thing missing and how many more, with every one there for a screen reader", async () => {
    renderEditor({ kind: "create" });
    await userEvent.clear(screen.getByLabelText("Emoji"));
    const save = screen.getByRole("button", { name: "Save and add to this row" });
    expect(screen.getByText("+2 more")).toBeInTheDocument();
    expect(save).toHaveAccessibleDescription("Add a name. Add an emoji. Add at least one tag, collection or film.");
  });

  it("names the days from Easter people know by name alone, short enough for a phone, and says how far they are", async () => {
    renderEditor({ kind: "create" });
    await userEvent.click(screen.getByRole("button", { name: "Days from Easter" }));
    const days = screen.getByLabelText("Days from Easter");
    expect(within(days).getByRole("option", { name: "Mothering Sunday" })).toBeInTheDocument();
    expect(within(days).getByRole("option", { name: "Easter Sunday" })).toBeInTheDocument();
    expect(within(days).getByRole("option", { name: "5 days after" })).toBeInTheDocument();
    expect(Math.max(...[...(days as HTMLSelectElement).options].map((o) => o.text.length))).toBeLessThanOrEqual(16);

    await userEvent.selectOptions(days, "-21");
    expect(screen.getByText(/Mothering Sunday is 21 days before Easter Sunday\./)).toBeInTheDocument();
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
