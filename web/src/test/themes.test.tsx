import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import {
  buildPrompt,
  missingFromServer,
  selectsNothing,
  themeGuidance,
  toSaveBody,
  useThemePreview,
} from "@/lib/themes";
import type { Theme, ThemePreview } from "@/lib/types";

const { previewTheme } = vi.hoisted(() => ({ previewTheme: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, previewTheme: (body: unknown) => previewTheme(body) } };
});

function theme(patch: Partial<Theme> = {}): Theme {
  return {
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
    content_hash: "abc",
    ai_tokens: 0,
    stats: {},
    ...patch,
  };
}

function preview(patch: Partial<ThemePreview> = {}): ThemePreview {
  return {
    draft: theme(),
    stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25, unwatched_median: null },
    diff: null,
    tokens: 321,
    ...patch,
  };
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  previewTheme.mockReset();
  previewTheme.mockImplementation(() => Promise.resolve(preview()));
});

describe("useThemePreview", () => {
  it("answers the same request from what it already has instead of spending tokens again", async () => {
    const { result } = renderHook(() => useThemePreview(), { wrapper });

    let first: Awaited<ReturnType<typeof result.current.build>> | undefined;
    let second: Awaited<ReturnType<typeof result.current.build>> | undefined;
    await act(async () => {
      first = await result.current.build({ brief: "films with a twist", media: "movie" });
    });
    await act(async () => {
      second = await result.current.build({ brief: "  films with a twist ", media: "movie" });
    });

    expect(previewTheme).toHaveBeenCalledTimes(1);
    expect(first?.cached).toBe(false);
    expect(second?.cached).toBe(true);
    expect(second?.preview.tokens).toBe(321);
  });

  it("sends the request as asked, with the brief trimmed", async () => {
    const { result } = renderHook(() => useThemePreview(), { wrapper });

    await act(async () => {
      await result.current.build({ brief: "  twists  ", media: "show", guidance: "Prefer old films.", collection_id: 7 });
    });

    expect(previewTheme).toHaveBeenCalledWith({
      brief: "twists",
      media: "show",
      guidance: "Prefer old films.",
      collection_id: 7,
    });
  });

  it.each([
    ["a different brief", { brief: "heists" }],
    ["different guidance", { brief: "films with a twist", guidance: "Only the 90s." }],
    ["a different change", { change: "less gore", current_theme_id: 9 }],
    ["a refinement of a stored theme", { brief: "films with a twist", current_theme_id: 9 }],
  ])("asks again for %s", async (_label, other) => {
    const { result } = renderHook(() => useThemePreview(), { wrapper });

    await act(async () => {
      await result.current.build({ brief: "films with a twist" });
    });
    await act(async () => {
      await result.current.build(other);
    });

    expect(previewTheme).toHaveBeenCalledTimes(2);
  });

  it("asks again once the stored theme has changed, even for the same refinement", async () => {
    const { result } = renderHook(() => useThemePreview(), { wrapper });
    const refine = { change: "less gore", current_theme_id: 9 };

    await act(async () => {
      await result.current.build(refine, "hash-1");
    });
    await act(async () => {
      await result.current.build(refine, "hash-2");
    });

    expect(previewTheme).toHaveBeenCalledTimes(2);
  });

  it("does not cache a failure, so Try again really tries again", async () => {
    previewTheme.mockRejectedValueOnce(new Error("boom"));
    const { result } = renderHook(() => useThemePreview(), { wrapper });

    await act(async () => {
      await result.current.build({ brief: "twists" }).catch(() => undefined);
    });
    await act(async () => {
      await result.current.build({ brief: "twists" });
    });

    expect(previewTheme).toHaveBeenCalledTimes(2);
  });
});

describe("themeGuidance", () => {
  const DEFAULT = "You curate themed rows.";

  it.each([
    [{ mode: "default" as const, text: "ignored" }, ""],
    [{ mode: "own" as const, text: "  Only 1990s films.  " }, "Only 1990s films."],
    [{ mode: "add" as const, text: "Avoid horror." }, "You curate themed rows. Avoid horror."],
    [{ mode: "add" as const, text: "  " }, ""],
  ])("turns %j into the guidance sent", (instructions, expected) => {
    expect(themeGuidance(instructions, DEFAULT)).toBe(expected);
  });
});

describe("buildPrompt", () => {
  it("joins the guidance and the locked mechanics the way the server does, falling back to the default", () => {
    expect(buildPrompt("", " Default. ", "Reply in JSON.")).toBe("Default. Reply in JSON.");
    expect(buildPrompt(" Mine. ", "Default.", "Reply in JSON.")).toBe("Mine. Reply in JSON.");
  });
});

describe("missingFromServer", () => {
  it("counts what TMDB knew but the libraries lack, never below zero", () => {
    expect(missingFromServer({ resolved: 40, in_library: 30 })).toBe(10);
    expect(missingFromServer({ resolved: 10, in_library: 14 })).toBe(0);
  });
});

describe("selectsNothing", () => {
  it("is true only when there are no tags, genres, collections or titles", () => {
    expect(selectsNothing(theme({ tags: [], genres: [], collections: [], picks: [] }))).toBe(true);
    expect(selectsNothing(theme({ tags: [], genres: ["horror"], collections: [], picks: [] }))).toBe(false);
  });
});

describe("toSaveBody", () => {
  it("sends the contents, the tokens and the row, and never the hash or slug", () => {
    const body = toSaveBody(
      { draft: theme({ media: ["movie", "show"] }), stats: preview().stats, origin: "ai" },
      { tokens: 500, collectionId: 4 },
    );

    expect(body).toEqual({
      draft: expect.objectContaining({ name: "Twist endings", origin: "ai", media: ["movie", "show"] }),
      tokens: 500,
      collection_id: 4,
      stats: { named: 60, resolved: 40, in_library: 30, after_rules: 25 },
    });
    expect(body.draft).not.toHaveProperty("content_hash");
    expect(body.draft).not.toHaveProperty("slug");
  });

  it("leaves the stats out of a hand edit", () => {
    const body = toSaveBody({ draft: theme(), stats: null, origin: "manual" }, { tokens: 0, collectionId: null });

    expect(body).not.toHaveProperty("stats");
    expect(body.draft.origin).toBe("manual");
    expect(body.collection_id).toBeNull();
  });
});
