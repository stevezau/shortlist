import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowSeasonsField } from "@/components/rows/row-seasons-field";
import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import type { SeasonRow } from "@/lib/season-verdict";
import { seasonDate } from "@/lib/seasons";

import {
  CATALOGUE,
  CHRISTMAS,
  FATHERS_DAY_AU,
  HALLOWEEN,
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
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return onChange;
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
    expect(within(item).getByText("People's rows will be much alike — works best in a shared row")).toBeInTheDocument();
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

  it("offers ready-made seasons with their dates and counts, and Add opens the editor filled in", async () => {
    renderField(ON);
    const presets = await screen.findByRole("list", { name: "Ready-made seasons" });
    const card = (await within(presets).findByText("Thanksgiving (US)")).closest("li") as HTMLElement;
    expect(within(card).getByText(/4th Thursday of November/)).toBeInTheDocument();
    expect(await within(card).findByText("26 films in your libraries")).toBeInTheDocument();
    expect(mocks.previewSeason).toHaveBeenCalledWith(expect.objectContaining({ media: "movie", library_keys: [] }));
    expect(within(presets).getByText("TMDB tags very few films as Father's Day — add a collection or your own picks.")).toBeInTheDocument();

    await userEvent.click(within(card).getByRole("button", { name: "Add Thanksgiving (US)" }));
    const dialog = await screen.findByRole("dialog", { name: "Add Thanksgiving (US)" });
    expect(within(dialog).getByLabelText("Season name")).toHaveValue("Thanksgiving");
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
    const dialog = await screen.findByRole("dialog", { name: "Add Thanksgiving (US)" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Save and add to this row" }));

    await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
    expect(onChange).toHaveBeenCalledWith({ seasons: ["halloween", "thanksgiving", "christmas"] });
    await waitFor(() => expect(within(presets).queryByText("Thanksgiving (US)")).toBeNull());
    expect(within(presets).getByText("Father's Day (AU, NZ)")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("checkbox", { name: /Thanksgiving/ })).toHaveFocus());
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
