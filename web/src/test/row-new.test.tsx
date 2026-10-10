import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter, Route, Routes } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { RowNewPage } from "@/pages/row-new";

const { getUsers, createCollection, getRequestRowSources } = vi.hoisted(() => ({
  getUsers: vi.fn(),
  createCollection: vi.fn(),
  getRequestRowSources: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getUsers: () => getUsers(),
      createCollection: (body: unknown) => createCollection(body),
      getRequestRowSources: () => getRequestRowSources(),
      getSettings: () => Promise.resolve({}),
      listCollections: () => Promise.resolve([]),
    },
  };
});

function person(id: number, username: string, over: Record<string, unknown> = {}) {
  return {
    id,
    username,
    display_name: username,
    slug: username,
    user_type: "shared",
    enabled: true,
    prefs: {},
    ...over,
  };
}

function renderPage(url = "/rows/new") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[url]}>
        <Routes>
          <Route path="/rows" element={<p>rows list</p>} />
          <Route path="/rows/new" element={<RowNewPage />} />
          <Route path="/rows/new/full" element={<p>full editor</p>} />
          <Route path="/rows/:id" element={<p>row editor</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("RowNewPage", () => {
  beforeEach(() => {
    getUsers.mockReset();
    createCollection.mockReset();
    getRequestRowSources.mockReset();
    getUsers.mockResolvedValue([person(1, "sarah"), person(2, "mike")]);
    createCollection.mockResolvedValue({ id: 9 });
    getRequestRowSources.mockResolvedValue({ overseerr: "ok", radarr: "off", sonarr: "off", problems: [] });
  });

  it("adds the starting kind with its template's settings, then opens the row to fine-tune", async () => {
    renderPage();
    await screen.findAllByText(/sarah, mike/);
    await userEvent.click(screen.getByRole("button", { name: "Add row" }));

    await waitFor(() => expect(createCollection).toHaveBeenCalledTimes(1));
    const body = createCollection.mock.calls[0]![0];
    expect(body).toMatchObject({
      name: "✨ {library_name} Picks",
      build: "per_person",
      audience: "everyone",
      audience_user_ids: [],
      size: 15,
      theme_id: null,
      hub_anchor: {},
    });
    expect(await screen.findByText("row editor")).toBeInTheDocument();
  });

  it("sums up a long roster as a count with a Show all disclosure", async () => {
    getUsers.mockResolvedValue(Array.from({ length: 12 }, (_, i) => person(i + 1, `viewer${i + 1}`)));
    renderPage();
    const preview = within(await screen.findByRole("complementary", { name: "Preview" }));
    expect(await preview.findByText(/12 people/)).toBeInTheDocument();
    expect(preview.queryByText(/viewer1, viewer2/)).not.toBeInTheDocument();
    await userEvent.click(preview.getByRole("button", { name: "Show all" }));
    expect(preview.getByText(/viewer1, viewer2/)).toBeInTheDocument();
  });

  it("starts from the kind and the name you pick", async () => {
    renderPage();
    await screen.findAllByText(/sarah, mike/);
    await userEvent.click(screen.getByRole("button", { name: /Because you watched/ }));
    const name = screen.getByRole("textbox", { name: "Row name" });
    expect(name).toHaveValue("🎯 Because you watched {top_seed}");

    await userEvent.clear(name);
    await userEvent.type(name, "Next up for {{user}");
    await userEvent.click(screen.getByRole("button", { name: "Add row" }));

    await waitFor(() => expect(createCollection).toHaveBeenCalledTimes(1));
    expect(createCollection.mock.calls[0]![0]).toMatchObject({ name: "Next up for {user}", media: "movie" });
  });

  it("shows what each person's row will be called", async () => {
    renderPage();
    const preview = (await screen.findByText("What each person sees")).closest("div")!;
    expect(within(preview).getByText("sarah")).toBeInTheDocument();
    expect(within(preview).getByText("mike")).toBeInTheDocument();
  });

  it("fills the library into a shared row's name instead of showing the token", async () => {
    const user = userEvent.setup();
    renderPage("/rows/new?template=popular-here");
    const field = await screen.findByLabelText("Row name");
    await user.clear(field);
    await user.type(field, "{{library_name} Picks");
    const preview = screen.getByText("What people see").closest("div")!;
    expect(within(preview).getByText("Movies Picks")).toBeInTheDocument();
    expect(within(preview).getByText("TV Shows Picks")).toBeInTheDocument();
    expect(field).toHaveValue("{library_name} Picks");
  });

  it("puts Rows in the title line instead of a back link above it", async () => {
    renderPage();
    const heading = await screen.findByRole("heading", { level: 1 });
    expect(within(heading).getByRole("link", { name: "Rows" })).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Rows" })).toHaveLength(1);
  });

  it("sends only the people you choose", async () => {
    renderPage();
    await screen.findAllByText(/sarah, mike/);
    const add = screen.getByRole("button", { name: "Add row" });
    await userEvent.click(screen.getByRole("button", { name: "Choose people" }));
    expect(add).toBeDisabled();

    await userEvent.click(await screen.findByRole("switch", { name: "mike" }));
    await userEvent.click(add);

    await waitFor(() => expect(createCollection).toHaveBeenCalledTimes(1));
    expect(createCollection.mock.calls[0]![0]).toMatchObject({ audience: "subset", audience_user_ids: [2] });
  });

  it("does not add a row without a name", async () => {
    renderPage();
    await userEvent.clear(await screen.findByRole("textbox", { name: "Row name" }));
    expect(screen.getByRole("button", { name: "Add row" })).toBeDisabled();
  });

  it("holds Your requests back when nothing can say who asked for what", async () => {
    getRequestRowSources.mockResolvedValue({ overseerr: "off", radarr: "off", sonarr: "off", problems: [] });
    renderPage("/rows/new?template=your-requests");
    expect(await screen.findByText(/Needs a way to know who asked for what/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Add row" })).toBeDisabled();
  });

  it("sends an AI row to the full editor, where its list is written", async () => {
    renderPage("/rows/new?template=describe-a-row");
    await userEvent.click(await screen.findByRole("button", { name: "Continue" }));
    expect(await screen.findByText("full editor")).toBeInTheDocument();
    expect(createCollection).not.toHaveBeenCalled();
  });

  it("keeps the full editor one link away", async () => {
    renderPage();
    await userEvent.click(await screen.findByRole("link", { name: "Set every option yourself" }));
    expect(await screen.findByText("full editor")).toBeInTheDocument();
  });
});
