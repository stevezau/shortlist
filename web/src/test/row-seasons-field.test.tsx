import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowSeasonsField } from "@/components/rows/row-seasons-field";
import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import { useCollections } from "@/lib/queries";
import type { SeasonRow } from "@/lib/season-verdict";
import { seasonDate } from "@/lib/seasons";

import {
  ANIMATION_MONTH,
  CATALOGUE,
  CHRISTMAS,
  FATHERS_DAY_AU,
  HALLOWEEN,
  STAR_WARS_DAY,
  THANKSGIVING,
  THANKSGIVING_US,
  VALENTINES,
  preview,
} from "./season-fixtures";

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
  listCollections: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: mocks };
});

type Value = { seasons: string[]; season_lead_days: number; season_after_days: number };

function renderField(
  value: Value,
  {
    schedule = "30 3 * * *",
    name = "{season_emoji} {season} picks",
    status = null as never,
    row = { size: 15, perPerson: true, media: "movie", libraryKeys: [] } as SeasonRow,
    // The row editor's page holds the rows list open, so a season save refetches it too.
    withRowsList = false,
  } = {},
) {
  const onChange = vi.fn();
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <RowSeasonsField
          value={value}
          onChange={onChange}
          schedule={schedule}
          name={name}
          status={status}
          row={row}
          savedRow={null}
        />
        {withRowsList && <RowsList />}
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return onChange;
}

function RowsList() {
  useCollections();
  return null;
}

const ON: Value = { seasons: ["halloween", "christmas"], season_lead_days: 30, season_after_days: 0 };

beforeEach(() => {
  for (const mock of Object.values(mocks)) mock.mockReset();
  mocks.getSeasons.mockResolvedValue(CATALOGUE);
  mocks.getSeasonPresets.mockResolvedValue([THANKSGIVING_US, FATHERS_DAY_AU]);
  mocks.previewSeason.mockResolvedValue(preview({ total: 26, from_tags: 26 }));
  mocks.getTmdbTags.mockResolvedValue([]);
  mocks.getPlexCollections.mockResolvedValue([]);
  mocks.searchLibrary.mockResolvedValue([]);
  mocks.getSeasonNextDate.mockResolvedValue({ next_date: "2026-11-26", rule_error: null });
});

describe("RowSeasonsField", () => {
  // "Follow the calendar" is gone: whether a row is seasonal is its kind now, picked in the row
  // editor. Turning it on — every season, a month ahead — is tested there
  // (`row-editor-kinds.test.tsx`, "turning it on follows every season, a month ahead").
  it("has no on/off switch of its own, and writes nothing on its own", async () => {
    const onChange = renderField(ON);
    await screen.findByRole("checkbox", { name: /Halloween/ });
    expect(screen.queryByRole("switch")).toBeNull();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("lists each season with its date, next day and when it would show", async () => {
    renderField(ON);
    const halloween = await screen.findByRole("checkbox", { name: /Halloween/ });
    expect(halloween).toBeChecked();
    expect(screen.getByRole("checkbox", { name: /Valentine/ })).not.toBeChecked();
    expect(halloween).toHaveAccessibleDescription(/31 October/);
    expect(halloween).toHaveAccessibleDescription(
      new RegExp(`Shows ${seasonDate("2026-10-01")} – ${seasonDate("2026-10-31")}, from 30 days before`),
    );
  });

  it("marks a built-in season as such, with no film count and no Edit", async () => {
    renderField(ON);
    const item = (await screen.findByRole("checkbox", { name: /Halloween/ })).closest("[data-season]");
    expect(item).not.toBeNull();
    expect(within(item as HTMLElement).getByText("Built in")).toBeInTheDocument();
    expect(within(item as HTMLElement).queryByRole("button", { name: /Edit/ })).toBeNull();
    expect(within(item as HTMLElement).queryByText(/^\d+ films?$/)).toBeNull();
  });

  it("lists a season of the owner's as Yours, with its own timing, its film count and Edit", async () => {
    renderField(ON);
    const box = await screen.findByRole("checkbox", { name: /Thanksgiving/ });
    const item = box.closest("[data-season]") as HTMLElement;
    expect(within(item).getByText("Yours")).toBeInTheDocument();
    expect(box).toHaveAccessibleDescription(/from 14 days before/);
    expect(await within(item).findByText("26 films")).toBeInTheDocument();
    expect(within(item).getByText("People's rows will be much alike. Choose Shared in the row editor to use the season's most-watched titles.")).toBeInTheDocument();
    expect(within(item).getByRole("button", { name: "Edit Thanksgiving" })).toBeInTheDocument();
  });

  it("counts a season for this row's media and libraries, and says shows for a shows row", async () => {
    renderField(ON, { row: { size: 15, perPerson: true, media: "show", libraryKeys: ["2"] } });
    const box = await screen.findByRole("checkbox", { name: /Thanksgiving/ });
    const item = box.closest("[data-season]") as HTMLElement;

    expect(await within(item).findByText("26 shows")).toBeInTheDocument();
    expect(mocks.previewSeason).toHaveBeenCalledWith(expect.objectContaining({ media: "show", library_keys: ["2"] }));
  });

  it("opens the editor on a season of the owner's from Edit", async () => {
    renderField(ON);
    await userEvent.click(await screen.findByRole("button", { name: "Edit Thanksgiving" }));
    const dialog = await screen.findByRole("dialog", { name: "Edit Thanksgiving" });
    expect(within(dialog).getByLabelText("Season name")).toHaveValue("Thanksgiving");
  });

  it("adds a season in calendar order", async () => {
    const onChange = renderField(ON);
    await userEvent.click(await screen.findByRole("checkbox", { name: /Valentine/ }));
    expect(onChange).toHaveBeenCalledWith({ seasons: ["valentines", "halloween", "christmas"] });
  });

  it("adds a season of the owner's in calendar order, between the built-ins", async () => {
    const onChange = renderField(ON);
    await userEvent.click(await screen.findByRole("checkbox", { name: /Thanksgiving/ }));
    expect(onChange).toHaveBeenCalledWith({ seasons: ["halloween", "thanksgiving", "christmas"] });
  });

  it("will not untick the last season, which would make it a row that follows none, and says why", async () => {
    const onChange = renderField({ ...ON, seasons: ["christmas"] });
    await userEvent.click(await screen.findByRole("checkbox", { name: /Christmas/ }));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText(/needs at least one season/)).toHaveAttribute("role", "status");
  });

  it("keeps the built-in days before inside the range the server accepts", async () => {
    const onChange = renderField(ON);
    const lead = await screen.findByLabelText(/Built-in seasons show from/);
    await userEvent.clear(lead);
    await userEvent.type(lead, "400");
    expect(onChange).toHaveBeenLastCalledWith({ season_lead_days: 90 });
  });

  it("says the row's timing is for built-in seasons", async () => {
    renderField(ON);
    const lead = await screen.findByLabelText(/Built-in seasons show from/);
    expect(lead.closest("p")).toHaveTextContent("Built-in seasons show from days before and stay days after.");
  });

  it("hides the timing when only seasons with their own timing are ticked", async () => {
    renderField({ ...ON, seasons: ["thanksgiving"] });
    await screen.findByRole("checkbox", { name: /Thanksgiving/ });
    expect(screen.queryByLabelText(/Built-in seasons show from/)).toBeNull();
    expect(screen.queryByLabelText(/and stay/)).toBeNull();
  });

  it("offers descriptions, dates, counts and up to three library samples, with optional Customise", async () => {
    mocks.previewSeason.mockResolvedValue(preview({ total: 26, from_tags: 26, sample: ["Free Birds", "The Ice Storm", "Home for the Holidays", "Fourth film"] }));
    renderField(ON);
    const presets = await screen.findByRole("list", { name: "Ready-made seasons" });
    const card = (await within(presets).findByText("Thanksgiving (US)")).closest("li") as HTMLElement;
    expect(within(card).getByText(/4th Thursday of November/)).toBeInTheDocument();
    expect(await within(card).findByText("26 films in your libraries")).toBeInTheDocument();
    expect(within(card).getByText(THANKSGIVING_US.description)).toBeInTheDocument();
    for (const title of ["Free Birds", "The Ice Storm", "Home for the Holidays"]) {
      expect(card).toHaveTextContent(title);
    }
    expect(card).not.toHaveTextContent("Fourth film");
    expect(mocks.previewSeason).toHaveBeenCalledWith(expect.objectContaining({ media: "movie", library_keys: [] }));
    expect(within(presets).getByText("TMDB tags very few films as Father's Day — add a collection or your own picks.")).toBeInTheDocument();

    await userEvent.click(within(card).getByRole("button", { name: "Customise Thanksgiving (US)" }));
    const dialog = await screen.findByRole("dialog", { name: "Add Thanksgiving (US)" });
    expect(within(dialog).getByLabelText("Season name")).toHaveValue("Thanksgiving");
    expect(mocks.createSeason).not.toHaveBeenCalled();
  });

  it("searches ready-made names and descriptions and filters every category", async () => {
    mocks.getSeasonPresets.mockResolvedValue([THANKSGIVING_US, STAR_WARS_DAY, ANIMATION_MONTH]);
    renderField(ON);
    const presets = await screen.findByRole("list", { name: "Ready-made seasons" });
    const search = screen.getByRole("searchbox", { name: "Find a season" });
    await userEvent.type(search, "generation");
    expect(within(presets).getByText("Animation month")).toBeInTheDocument();
    expect(within(presets).queryByText("Star Wars Day")).toBeNull();
    await userEvent.clear(search);
    await userEvent.type(search, "star wars");
    expect(within(presets).getByText("Star Wars Day")).toBeInTheDocument();
    expect(within(presets).queryByText("Animation month")).toBeNull();
    await userEvent.clear(search);

    for (const [category, title] of [["Holidays", "Thanksgiving (US)"], ["Film days", "Star Wars Day"], ["Spotlights", "Animation month"]] as const) {
      await userEvent.click(screen.getByRole("button", { name: category }));
      expect(within(presets).getByText(title)).toBeInTheDocument();
      expect(within(presets).getAllByRole("listitem")).toHaveLength(1);
    }
    await userEvent.click(screen.getByRole("button", { name: "All" }));
    expect(within(presets).getAllByRole("listitem")).toHaveLength(3);
    await userEvent.type(search, "no matching season");
    expect(screen.getByText(/no seasons match/i)).toBeInTheDocument();
  });

  it("adds the preset unchanged without an editor, and reminds the owner to save the row", async () => {
    mocks.getSeasons.mockResolvedValue([VALENTINES, HALLOWEEN, CHRISTMAS]);
    mocks.createSeason.mockImplementation(() => {
      mocks.getSeasons.mockResolvedValue(CATALOGUE);
      return Promise.resolve(THANKSGIVING);
    });
    const onChange = renderField(ON);
    await userEvent.click(await screen.findByRole("button", { name: "Add Thanksgiving (US)" }));
    await waitFor(() => expect(mocks.createSeason).toHaveBeenCalledTimes(1));
    expect(mocks.createSeason).toHaveBeenCalledWith({
      name: THANKSGIVING_US.name,
      emoji: THANKSGIVING_US.emoji,
      preset: THANKSGIVING_US.key,
      rule: THANKSGIVING_US.rule,
      lead_days: THANKSGIVING_US.lead_days,
      after_days: THANKSGIVING_US.after_days,
      tags: THANKSGIVING_US.tags,
      genre: THANKSGIVING_US.genre,
      excluded_genres: THANKSGIVING_US.excluded_genres,
      collections: THANKSGIVING_US.collections,
      picks: THANKSGIVING_US.picks,
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ seasons: ["halloween", "thanksgiving", "christmas"] }));
    expect(screen.getByText(/save (this |the )?row/i)).toHaveAttribute("role", "status");
  });

  it("starts with six presets and searches the whole catalogue before expanding it", async () => {
    mocks.getSeasonPresets.mockResolvedValue(Array.from({ length: 8 }, (_, index) => ({
      ...THANKSGIVING_US,
      key: `season_${index}`,
      label: `Ready-made ${index}`,
      description: `Description ${index}`,
    })));
    renderField(ON);
    const presets = await screen.findByRole("list", { name: "Ready-made seasons" });
    expect(within(presets).getAllByRole("listitem")).toHaveLength(6);
    await userEvent.type(screen.getByRole("searchbox", { name: "Find a season" }), "Ready-made 7");
    expect(within(presets).getByText("Ready-made 7")).toBeInTheDocument();
    await userEvent.clear(screen.getByRole("searchbox", { name: "Find a season" }));
    await userEvent.click(screen.getByRole("button", { name: "Show all 8 seasons" }));
    expect(within(presets).getAllByRole("listitem")).toHaveLength(8);
  });

  it("explains a film-only preset on a TV row and leaves Customise available", async () => {
    mocks.getSeasonPresets.mockResolvedValue([{
      ...STAR_WARS_DAY,
      genre: null,
      picks: [{ tmdb_id: 11, media_type: "movie", title: "Star Wars", year: 1977 }],
    }]);
    renderField(ON, { row: { size: 15, perPerson: false, media: "show", libraryKeys: ["2"] } });
    const presets = await screen.findByRole("list", { name: "Ready-made seasons" });
    expect(within(presets).getByRole("button", { name: "Add Star Wars Day" })).toBeDisabled();
    expect(within(presets).getByText(/Films only/)).toBeInTheDocument();
    expect(within(presets).getByRole("button", { name: "Customise Star Wars Day" })).toBeEnabled();
    expect(mocks.createSeason).not.toHaveBeenCalled();
  });

  it("blocks duplicate creates while Add is pending", async () => {
    mocks.createSeason.mockReturnValue(new Promise(() => {}));
    renderField(ON);
    const add = await screen.findByRole("button", { name: "Add Thanksgiving (US)" });
    await userEvent.dblClick(add);
    expect(mocks.createSeason).toHaveBeenCalledTimes(1);
    expect(add).toBeDisabled();
    expect(screen.getByRole("button", { name: "Add Father's Day (AU, NZ)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Customise Thanksgiving (US)" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Create your own" })).toBeDisabled();
  });

  it("keeps a failed Add beside the preset and retries it without ticking the row early", async () => {
    mocks.createSeason.mockRejectedValueOnce(new ApiError(503, "Shortlist is temporarily unavailable."));
    const onChange = renderField(ON);
    const add = await screen.findByRole("button", { name: "Add Thanksgiving (US)" });
    const card = add.closest("li") as HTMLElement;
    await userEvent.click(add);
    expect(await within(card).findByText(/Shortlist is temporarily unavailable/)).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
    mocks.createSeason.mockResolvedValue(THANKSGIVING);
    await userEvent.click(within(card).getByRole("button", { name: /Retry/ }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ seasons: ["halloween", "thanksgiving", "christmas"] }));
    expect(mocks.createSeason).toHaveBeenCalledTimes(2);
  });

  it("offers Customise when Add fails because another season has the same name", async () => {
    mocks.createSeason.mockRejectedValue(new ApiError(422, "There's already a season called “Thanksgiving”."));
    renderField(ON);
    const add = await screen.findByRole("button", { name: "Add Thanksgiving (US)" });
    const card = add.closest("li") as HTMLElement;
    await userEvent.click(add);
    expect(await within(card).findByText(/already a season called/)).toBeInTheDocument();
    await userEvent.click(within(card).getByRole("button", { name: "Customise Thanksgiving (US)" }));
    expect(await screen.findByRole("dialog", { name: "Add Thanksgiving (US)" })).toBeInTheDocument();
  });

  it("says so when every ready-made season has been added", async () => {
    mocks.getSeasonPresets.mockResolvedValue([]);
    renderField(ON);
    expect(await screen.findByText("You've added every ready-made season.")).toBeInTheDocument();
  });

  it("keeps the ready-made seasons folded away on a row that already has one of the owner's", async () => {
    renderField({ ...ON, seasons: ["thanksgiving", "christmas"] });
    await screen.findByRole("checkbox", { name: /Thanksgiving/ });
    const details = screen.getByText("Add more seasons").closest("details");
    expect(details).not.toHaveAttribute("open");
  });

  it("ticks a season saved from Create your own, in calendar order", async () => {
    const turkey = { ...THANKSGIVING, slug: "turkey-day", name: "Turkey day", used_by: [] };
    mocks.searchLibrary.mockResolvedValue([{ tmdb_id: 11, media_type: "movie", title: "Free Birds", year: 2013 }]);
    mocks.createSeason.mockImplementation(() => {
      // The server's catalogue carries it from now on, between Halloween and Christmas.
      mocks.getSeasons.mockResolvedValue([VALENTINES, HALLOWEEN, turkey, CHRISTMAS]);
      return Promise.resolve(turkey);
    });
    const onChange = renderField(ON);

    await userEvent.click(await screen.findByRole("button", { name: "Create your own" }));
    const dialog = await screen.findByRole("dialog", { name: "Create your own season" });
    await userEvent.type(within(dialog).getByLabelText("Season name"), "Turkey day");
    await userEvent.type(within(dialog).getByLabelText("Search your libraries"), "Free");
    await userEvent.click(await within(dialog).findByRole("button", { name: "Add Free Birds (2013)" }));
    await userEvent.click(within(dialog).getByRole("button", { name: "Save and add to this row" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ seasons: ["halloween", "turkey-day", "christmas"] }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  });

  it("takes a season the server no longer has off the row, so saving it isn't refused", async () => {
    // Deleted in another tab: Thanksgiving is ticked here but gone from the catalogue.
    mocks.getSeasons.mockResolvedValue([VALENTINES, HALLOWEEN, CHRISTMAS]);
    const onChange = renderField({ ...ON, seasons: ["halloween", "thanksgiving"] });
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ seasons: ["halloween"] }));
  });

  it("asks for another season when every one the row ticked has been deleted", async () => {
    mocks.getSeasons.mockResolvedValue([VALENTINES, HALLOWEEN, CHRISTMAS]);
    const onChange = renderField({ ...ON, seasons: ["thanksgiving"] });
    expect(await screen.findByText(/The season this row followed has been deleted/)).toBeInTheDocument();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("unticks a deleted season in the form, and puts focus on the Seasons heading", async () => {
    mocks.deleteSeason.mockResolvedValue(undefined);
    const onChange = renderField({ ...ON, seasons: ["thanksgiving", "christmas"] });
    await userEvent.click(await screen.findByRole("button", { name: "Edit Thanksgiving" }));
    await screen.findByRole("dialog", { name: "Edit Thanksgiving" });
    await userEvent.click(screen.getByRole("button", { name: "Delete season" }));
    const confirm = await screen.findByRole("dialog", { name: "Delete “Thanksgiving”?" });
    await userEvent.click(within(confirm).getByRole("button", { name: "Delete season" }));

    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ seasons: ["christmas"] }));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    await waitFor(() => expect(screen.getByRole("heading", { name: "Seasons" })).toHaveFocus());
    expect(screen.getByText("Deleted “🦃 Thanksgiving” and unticked it here.")).toHaveAttribute("role", "status");
  });

  it("drops a ready-made season's card once it is saved, ticks it, and puts focus on its checkbox", async () => {
    mocks.getSeasons.mockResolvedValue([VALENTINES, HALLOWEEN, CHRISTMAS]);
    mocks.createSeason.mockImplementation(() => {
      mocks.getSeasons.mockResolvedValue(CATALOGUE);
      mocks.getSeasonPresets.mockResolvedValue([FATHERS_DAY_AU]);
      return Promise.resolve(THANKSGIVING);
    });
    const onChange = renderField(ON);
    const presets = await screen.findByRole("list", { name: "Ready-made seasons" });
    await userEvent.click(await within(presets).findByRole("button", { name: "Add Thanksgiving (US)" }));
    await waitFor(() => expect(mocks.createSeason).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onChange).toHaveBeenCalledWith({ seasons: ["halloween", "thanksgiving", "christmas"] });
    await waitFor(() => expect(within(presets).queryByText("Thanksgiving (US)")).toBeNull());
    expect(within(presets).getByText("Father's Day (AU, NZ)")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Thanksgiving/ })).toHaveFocus());
  });

  it("finishes adding without waiting for the rows list, which a running run can hold up for seconds", async () => {
    mocks.getSeasons.mockResolvedValue([VALENTINES, HALLOWEEN, CHRISTMAS]);
    mocks.listCollections.mockResolvedValueOnce([]);
    mocks.createSeason.mockImplementation(() => {
      mocks.getSeasons.mockResolvedValue(CATALOGUE);
      mocks.listCollections.mockReturnValue(new Promise(() => {}));
      return Promise.resolve(THANKSGIVING);
    });
    const onChange = renderField(ON, { withRowsList: true });
    await waitFor(() => expect(mocks.listCollections).toHaveBeenCalledTimes(1));
    await userEvent.click(await screen.findByRole("button", { name: "Add Thanksgiving (US)" }));
    await waitFor(() => expect(onChange).toHaveBeenCalledWith({ seasons: ["halloween", "thanksgiving", "christmas"] }));
    expect(mocks.listCollections).toHaveBeenCalledTimes(2);
  });

  it("puts focus on the added season's checkbox without scrolling the page away from the presets", async () => {
    mocks.getSeasons.mockResolvedValue([VALENTINES, HALLOWEEN, CHRISTMAS]);
    mocks.createSeason.mockImplementation(() => {
      mocks.getSeasons.mockResolvedValue(CATALOGUE);
      return Promise.resolve(THANKSGIVING);
    });
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    renderField(ON);
    await userEvent.click(await screen.findByRole("button", { name: "Add Thanksgiving (US)" }));
    const box = await screen.findByRole("checkbox", { name: /Thanksgiving/ });
    await waitFor(() => expect(box).toHaveFocus());
    const onBox = focus.mock.contexts.flatMap((element, i) => (element === box ? [focus.mock.calls[i]] : []));
    expect(onBox).toEqual([[{ preventScroll: true }]]);
    focus.mockRestore();
  });

  it("gives the server's reason when a count fails, and Settings rather than Retry for a missing TMDB key", async () => {
    mocks.previewSeason.mockRejectedValue(new ApiError(503, "Add a TMDB API key in Settings first."));
    renderField(ON);
    const item = (await screen.findByRole("checkbox", { name: /Thanksgiving/ })).closest("[data-season]") as HTMLElement;
    expect(await within(item).findByText(/Add a TMDB API key in Settings first\./)).toBeInTheDocument();
    expect(within(item).getByRole("link", { name: "Open Settings in a new tab" })).toHaveAttribute(
      "href",
      "/settings#connections",
    );
    expect(within(item).queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("offers Retry when a count fails in a way that may pass next time", async () => {
    mocks.previewSeason.mockRejectedValue(new ApiError(502, "HTTPError: TMDB timed out"));
    renderField(ON);
    const item = (await screen.findByRole("checkbox", { name: /Thanksgiving/ })).closest("[data-season]") as HTMLElement;
    expect(await within(item).findByText(/HTTPError: TMDB timed out/)).toBeInTheDocument();
    mocks.previewSeason.mockResolvedValue(preview({ total: 26, from_tags: 26 }));
    await userEvent.click(within(item).getByRole("button", { name: "Retry" }));
    expect(await within(item).findByText("26 films")).toBeInTheDocument();
  });

  it("warns when the row does not run every night", () => {
    renderField(ON, { schedule: "30 3 * * 0" });
    expect(screen.getByText(/doesn’t run every night/)).toHaveTextContent(/nightly/i);
  });

  it("suggests putting the season in the name when it is not there", () => {
    renderField(ON, { name: "Picks for {user}" });
    expect(screen.getByText(/\{season\}/)).toBeInTheDocument();
  });

  it("says where a saved row is in its calendar, from the server", () => {
    renderField(ON, {
      status: {
        showing: null,
        next: { slug: "christmas", name: "Christmas", emoji: "🎄", starts: "2026-11-25", ends: "2026-12-25" },
      } as never,
    });
    expect(screen.getByText(`Hidden until 🎄 Christmas starts on ${seasonDate("2026-11-25")}`)).toBeInTheDocument();
  });
});
