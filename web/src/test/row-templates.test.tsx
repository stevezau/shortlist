import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowTemplateGallery } from "@/components/rows/row-template-gallery";
import { RowEditor } from "@/components/rows/row-editor";
import type * as ApiModule from "@/lib/api";
import { blankInput } from "@/lib/collections";
import {
  ROW_TEMPLATE_GROUPS,
  ROW_TEMPLATES,
  findRowTemplate,
  sentenceCaseHighlights,
} from "@/lib/row-templates";

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      createCollection: vi.fn(() => Promise.resolve({ id: 1 })),
      getSettings: () => Promise.resolve({}),
      getLibraries: () => Promise.resolve([]),
      getImageProvider: () =>
        Promise.resolve({ capable: false, provider: "", reason: "" }),
      getRequestRowSources: () => Promise.resolve(rowSources.current),
    },
  };
});

const { rowSources } = vi.hoisted(() => ({
  // What the gallery reads to decide whether the Your requests tile can be picked. Mutable so a
  // test can take every source away.
  rowSources: {
    current: {
      overseerr: "connected",
      radarr: "off",
      sonarr: "off",
      complete: true,
      problems: [],
      seerr_requests: 3,
      seerr_requesters: 2,
      seerr_linked: 2,
      servers: [],
      tagged_movies: 0,
      tagged_shows: 0,
      people: [],
      tags: [],
    },
  },
}));

beforeEach(() => {
  rowSources.current = { ...rowSources.current, overseerr: "connected" };
});

function renderGallery(onPick = vi.fn()) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <RowTemplateGallery open onPick={onPick} onClose={() => {}} />
      </QueryClientProvider>
    </MemoryRouter>,
  );
  return onPick;
}

describe("the Seasonal template", () => {
  it("follows every season as a nightly films row named after the season", () => {
    const seasonal = findRowTemplate("seasonal");
    expect(seasonal).toBeDefined();
    expect(seasonal!.values).toMatchObject({
      name: "{season_emoji} {season} picks",
      build: "per_person",
      media: "movie",
      seasons: ["valentines", "halloween", "christmas"],
      season_lead_days: 30,
      season_after_days: 0,
      refresh_days: 1,
      recency: 0,
    });
  });
});

describe("ROW_TEMPLATES", () => {
  it("only sets fields the row input actually has", () => {
    // A template that names a field the API doesn't accept would 422 on save with no clue why.
    const allowed = new Set(Object.keys(blankInput()));
    for (const template of ROW_TEMPLATES) {
      for (const key of Object.keys(template.values)) {
        expect(allowed, `${template.id} sets unknown field "${key}"`).toContain(
          key,
        );
      }
    }
  });

  it("gives every template a unique id and something to say about itself", () => {
    const ids = ROW_TEMPLATES.map((t) => t.id);
    expect(new Set(ids).size).toBe(ids.length);
    for (const template of ROW_TEMPLATES) {
      expect(template.highlights.length).toBeGreaterThan(0);
      expect(template.blurb.length).toBeGreaterThan(0);
    }
  });

  it("the requests template name is unique among templates", () => {
    // Two rows delivered under one title into one library are told apart by nothing: the removal
    // paths match on title, so a template sharing a name with another would have the requests row's
    // empty-night removal take the other row's collection with it.
    const names = ROW_TEMPLATES.map((t) => t.values.name);
    expect(new Set(names).size).toBe(names.length);
    expect(names).toContain("📬 {library_name} you asked for");
  });

  it("every template actually changes how the row behaves, not just its name", () => {
    // A template whose only difference is a title is a lie dressed as a feature: it promises a
    // distinct kind of row and produces the default one. Every tile must move at least one knob the
    // engine reads.
    const behavioural = [
      "build",
      "media",
      "size",
      "min_watchers",
      "watched_pct",
      "refresh_days",
      "recent_count",
      "max_seeds",
      "candidate_sources",
      "audience",
    ] as const;
    const blank = blankInput();

    for (const template of ROW_TEMPLATES) {
      const moved = behavioural.filter(
        (key) =>
          key in template.values &&
          JSON.stringify(template.values[key]) !== JSON.stringify(blank[key]),
      );
      // "Picked for You" is the deliberate exception: it IS the everyday defaults, and its whole
      // point is to be the plain starting row.
      if (template.id === "picked-for-you") continue;
      expect(
        moved.length,
        `${template.id} only changes its name`,
      ).toBeGreaterThan(0);
    }
  });

  it("gives every template a name, so nothing saves blank", () => {
    for (const template of ROW_TEMPLATES) {
      expect(template.values.name, template.id).toBeTruthy();
    }
  });

  it("puts the delivered title in `name`, not a parallel `name_template`", () => {
    // Two fields for one idea is what hid the emoji: the editor's Name box binds to `name`, so a
    // template filling `name_template` instead showed a plain title while delivering an emoji one.
    // Worse, `name_template || name` means the hidden field WINS — so editing the visible Name box
    // had no effect on what Plex actually showed.
    for (const template of ROW_TEMPLATES) {
      expect(
        template.values.name_template,
        `${template.id} still sets name_template`,
      ).toBeUndefined();
    }
  });

  it("puts a real variable in every title, and only ones the engine renders", () => {
    // `render_row_name` (engine/delivery.py) substitutes EXACTLY these. Anything else survives
    // verbatim onto a Plex shelf — "🌱 New {genre} to try" would ship with the braces showing. The two
    // season placeholders are filled on a seasonal row only, so a template using them must follow seasons.
    const SUPPORTED = ["{user}", "{library_name}", "{top_seed}"];
    const SEASONAL = ["{season}", "{season_emoji}"];

    for (const template of ROW_TEMPLATES) {
      const title = template.values.name ?? "";
      const used = [...title.matchAll(/\{[^}]+\}/g)].map((m) => m[0]);

      // A row builds one collection PER LIBRARY, so a title with no variable gives a `show` row two
      // identically-named collections (Sports and TV Shows) with nothing to tell them apart.
      expect(
        used.length,
        `${template.id} has no variable in its title`,
      ).toBeGreaterThan(0);
      for (const placeholder of used) {
        const seasonal = (template.values.seasons ?? []).length > 0;
        expect(
          seasonal ? [...SUPPORTED, ...SEASONAL] : SUPPORTED,
          `${template.id} uses "${placeholder}"`,
        ).toContain(placeholder);
      }
    }
  });

  it("means what it says about cadence", () => {
    // A template whose blurb promises "weekly" must carry the cadence that IS weekly. This needed a
    // fraction->days conversion here when the field was a 0..1 mood, and the conversion is what
    // caught "refreshed weekly" being a day out: 0.5 resolved to 8, not 7. The value says it now.
    expect(findRowTemplate("movie-night")!.values.refresh_days).toBe(7);
    // "Rebuilds nightly" and "Never rebuilds on its own" are the two ends, and both are exact.
    expect(findRowTemplate("fresh-finds")!.values.refresh_days).toBe(1);
    expect(findRowTemplate("from-the-vault")!.values.refresh_days).toBe(0);
  });

  it("keeps a {top_seed} row down to the one watch it names", () => {
    // The whole point of the template: at the default budget the row names one watch and fills
    // itself from the other 29, so the title claims something the contents don't honour.
    const template = findRowTemplate("because-you-watched");
    expect(template?.values.name).toContain("{top_seed}");
    expect(template?.values.max_seeds).toBe(1);
    // A single watch is a movie OR a show, so a "both" row at 1 seed leaves half of it empty.
    expect(template?.values.media).not.toBe("both");
  });
});

describe("row template kinds and grouping", () => {
  it("gives every template a kind", () => {
    for (const template of ROW_TEMPLATES) {
      expect(template.kind, template.id).toBeTruthy();
    }
  });

  it("groups templates into the five kinds, in order, with the right members", () => {
    const expected: {
      kind: string;
      heading: string;
      description: string;
      ids: string[];
    }[] = [
      {
        kind: "picked",
        heading: "Picked for You",
        description:
          "Titles they haven't seen yet, matched to everything they like.",
        ids: [
          "picked-for-you",
          "fresh-finds",
          "from-the-vault",
          "movie-night",
          "more-tv",
        ],
      },
      {
        kind: "byw",
        heading: "Because you watched",
        description:
          'More like one thing they watched recently. Named after it, like "Because you watched Dune".',
        ids: ["because-you-watched"],
      },
      {
        kind: "again",
        heading: "Watch it again",
        description:
          "Favourites they've already finished, ready to rewatch.",
        ids: ["seen-it-already"],
      },
      {
        kind: "requests",
        heading: "Your requests",
        description:
          "What they asked for in Overseerr that's now on Plex, newest first. Never recommendations.",
        ids: ["your-requests"],
      },
      {
        kind: "seasonal",
        heading: "Seasonal",
        description:
          "Only appears around the holidays you pick, like Halloween or Christmas. Filled in any of the ways above.",
        ids: ["seasonal"],
      },
      {
        kind: "popular",
        heading: "Popular on this server",
        description:
          "What lots of people here are watching. Everyone sees the same row.",
        ids: ["popular-here"],
      },
    ];

    expect(ROW_TEMPLATE_GROUPS.map((g) => g.kind)).toEqual(
      expected.map((g) => g.kind),
    );
    expected.forEach((group) => {
      const actual = ROW_TEMPLATE_GROUPS.find((g) => g.kind === group.kind);
      expect(actual?.heading).toBe(group.heading);
      expect(actual?.description).toBe(group.description);
      const members = ROW_TEMPLATES.filter((t) => t.kind === group.kind).map(
        (t) => t.id,
      );
      expect(members).toEqual(group.ids);
    });

    // Every template belongs to exactly one of the six groups — none left out, none doubled up.
    const grouped = expected.flatMap((g) => g.ids);
    expect(new Set(grouped).size).toBe(ROW_TEMPLATES.length);
  });

  it("renames the rewatch template without touching its id or preset", () => {
    const template = findRowTemplate("seen-it-already");
    expect(template?.title).toBe("Watch it again");
    expect(template?.values.name).toBe(
      "☕ {library_name} you've already seen",
    );
  });
});

describe("RowTemplateGallery", () => {
  it("offers every template plus a way to skip them", async () => {
    const onPick = renderGallery();

    for (const template of ROW_TEMPLATES) {
      // getAllByText, not getByText: a kind heading and its one template can share exact wording
      // (e.g. "Watch it again" is both the "again" group's heading and its only card's title).
      expect(screen.getAllByText(template.title).length).toBeGreaterThan(0);
    }

    await userEvent.click(
      screen.getByRole("button", { name: /Start from scratch/i }),
    );
    expect(onPick).toHaveBeenCalledWith(null);
  });

  it("shows a heading and one-line description for each kind", () => {
    renderGallery();

    for (const group of ROW_TEMPLATE_GROUPS) {
      expect(
        screen.getByRole("heading", { name: group.heading }),
      ).toBeInTheDocument();
      expect(screen.getByText(group.description)).toBeInTheDocument();
    }
  });

  it("explains templates as starting points you can change afterwards", () => {
    renderGallery();

    expect(
      screen.getByText(
        "Pick a starting point. It fills in the settings for you; you can change any of them, including the kind of row, afterwards.",
      ),
    ).toBeInTheDocument();
  });

  it("hands back the template that was clicked", async () => {
    const onPick = renderGallery();

    await userEvent.click(
      screen.getByRole("button", { name: /Watch it again/i }),
    );

    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({ id: "seen-it-already" }),
    );
  });

  it("offers Your requests while something can say who asked for what", async () => {
    const onPick = renderGallery();
    const tile = await screen.findByRole("button", { name: /Your requests/i });
    await userEvent.click(tile);
    expect(onPick).toHaveBeenCalledWith(
      expect.objectContaining({ id: "your-requests" }),
    );
  });

  it("disables Your requests when every source is off, and says what to set up", async () => {
    rowSources.current = { ...rowSources.current, overseerr: "off" };
    renderGallery();
    expect(
      await screen.findByText(
        /Needs a way to know who asked for what: an Overseerr or Jellyseerr connection, or Radarr\/Sonarr with request tags\./,
      ),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Your requests/i })).toBeNull();
    expect(screen.getByRole("link", { name: /Settings/ })).toHaveAttribute(
      "href",
      "/settings#connections",
    );
  });
});

describe("RowEditor seeded from a template", () => {
  function renderEditor(templateId: string) {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <RowEditor
            collection={null}
            template={findRowTemplate(templateId) ?? null}
            users={[]}
            onClose={() => {}}
          />
        </QueryClientProvider>
      </MemoryRouter>,
    );
  }

  it("prefills the fields the template sets", () => {
    renderEditor("seen-it-already");

    // The emoji and the variable land in the box the owner actually edits — the original complaint.
    expect(screen.getByLabelText(/^Name$/i)).toHaveValue(
      "☕ {library_name} you've already seen",
    );
    // `rewatch` lands as the row's kind, with its own setting on show.
    expect(screen.getByRole("radio", { name: "Watch it again" })).toBeChecked();
    expect(screen.getByLabelText(/Skip titles finished in the last/i)).toBeInTheDocument();
    // watched_pct 1 is still prefilled, but a rewatch row hides its slider: the engine ignores the
    // ceiling there (rows.py `effective_watched_pct … or spec.rewatch`).
    expect(findRowTemplate("seen-it-already")?.values.watched_pct).toBe(1);
    expect(
      screen.queryByRole("slider", {
        name: /Maximum share of the row that may be already-watched/i,
      }),
    ).not.toBeInTheDocument();
  });

  it("turns on the engine setting each template's promise depends on", () => {
    // Both were hollow before: "Watch it again" needed `rewatch` (watched_pct is only a ceiling,
    // so it never PROMOTES a finished title) and "More TV to watch" needed `unstarted_only` (the
    // normal filter only drops FINISHED shows).
    expect(findRowTemplate("seen-it-already")?.values.rewatch).toBe(true);
    expect(findRowTemplate("more-tv")?.values.unstarted_only).toBe(true);
  });

  it("says which template it started from, so prefilled fields aren't a mystery", () => {
    renderEditor("fresh-finds");

    expect(screen.getByText(/Started from/)).toBeInTheDocument();
    expect(screen.getByText(/🌱 Fresh finds/)).toBeInTheDocument();
  });

  it("says nothing about templates when editing from scratch", () => {
    const client = new QueryClient({
      defaultOptions: { queries: { retry: false } },
    });
    render(
      <MemoryRouter>
        <QueryClientProvider client={client}>
          <RowEditor collection={null} users={[]} onClose={() => {}} />
        </QueryClientProvider>
      </MemoryRouter>,
    );

    expect(screen.queryByText(/Started from/)).toBeNull();
    expect(screen.getByLabelText(/^Name$/i)).toHaveValue("");
  });
});

describe("what the row list says about a template's row", () => {
  it("badges a rewatch row by what it IS, not by the cap that enables it", async () => {
    // "Watched: no filter" describes plumbing — on a rewatch row watched_pct only stops the pool
    // dropping finished titles. Showing both would read as two competing settings.
    const { rowOverrides } = await import("@/lib/collections");
    const base = {
      ...blankInput(),
      rewatch: true,
      watched_pct: 1,
    };
    const parts = rowOverrides(
      base as unknown as Parameters<typeof rowOverrides>[0],
      null,
    );

    // Worded as the row editor's own switch is. "Rewatches first" was our name for the ordering
    // rule and meant nothing on a card (audit finding, Sep 2026).
    expect(parts).toContain("“Watch it again” row");
    expect(parts.some((p) => /Watched:/.test(p))).toBe(false);
  });

  it("names the scope of each seed-count badge, so two counts of watches cannot be confused", async () => {
    // These read "Recent watches: 3" and "Built from 1 watch" — two numbers of watches on one card,
    // neither saying what counted them. One governs every source; the other is the slice of it the
    // AI web search looks up (`candidates.py` searches `seeds[:recent_count]`).
    const { rowOverrides } = await import("@/lib/collections");
    const parts = rowOverrides(
      {
        ...blankInput(),
        max_seeds: 30,
        recent_count: 3,
      } as unknown as Parameters<typeof rowOverrides>[0],
      null,
    );

    expect(parts).toContain("All sources: 30 watches");
    expect(parts).toContain("AI web search: 3 watches");
    // The broader one leads, as it does in Settings — the narrower is a slice of it.
    expect(parts.indexOf("All sources: 30 watches")).toBeLessThan(
      parts.indexOf("AI web search: 3 watches"),
    );
  });

  it("keeps the singular for a one-watch row", async () => {
    const { rowOverrides } = await import("@/lib/collections");
    const parts = rowOverrides(
      {
        ...blankInput(),
        max_seeds: 1,
        recent_count: 1,
      } as unknown as Parameters<typeof rowOverrides>[0],
      null,
    );

    expect(parts).toContain("All sources: 1 watch");
    expect(parts).toContain("AI web search: 1 watch");
  });

  it("badges an unstarted-only row", async () => {
    const { rowOverrides } = await import("@/lib/collections");
    const parts = rowOverrides(
      { ...blankInput(), unstarted_only: true } as unknown as Parameters<
        typeof rowOverrides
      >[0],
      null,
    );
    expect(parts).toContain("Never started only");
  });
});

describe("sentenceCaseHighlights", () => {
  it("lowercases each highlight's first letter, except a proper noun or an acronym", () => {
    // The editor's "Started from …" banner joins the highlights into one sentence. A blanket
    // toLowerCase() wrote "overseerr or radarr/sonarr tags" and "tv only".
    expect(
      sentenceCaseHighlights(["Rebuilds nightly", "TV only", "Overseerr or Radarr/Sonarr tags"]),
    ).toEqual(["rebuilds nightly", "TV only", "Overseerr or Radarr/Sonarr tags"]);
    expect(sentenceCaseHighlights(["Plex only", "TMDB picks", "AI-ranked"])).toEqual([
      "Plex only",
      "TMDB picks",
      "AI-ranked",
    ]);
  });

  it("keeps the season names capitalised, as the Seasonal template's highlight leads with one", () => {
    // The Seasonal banner read "halloween, Christmas & Valentine's" — the first word lowercased, the
    // rest untouched, which is the worst of both.
    const seasonal = ROW_TEMPLATES.find((template) => template.kind === "seasonal")!;
    expect(seasonal.highlights).toContain("Halloween, Christmas & Valentine's");
    expect(sentenceCaseHighlights(["Halloween, Christmas & Valentine's", "Christmas only", "Valentine's Day"])).toEqual([
      "Halloween, Christmas & Valentine's",
      "Christmas only",
      "Valentine's Day",
    ]);
  });
});
