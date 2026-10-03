import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RecommendationsSection } from "@/components/settings/recommendations-section";
import type { Settings } from "@/lib/types";

const { putSettings, previewWebPrompt } = vi.hoisted(() => ({
  putSettings: vi.fn((values: Settings) => Promise.resolve(values)),
  previewWebPrompt: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: { putSettings, previewWebPrompt, testConnection: vi.fn() },
}));

function renderSection(settings: Settings, expand = true) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <RecommendationsSection settings={settings} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
  if (!expand) return;
  // These tests exercise the full controls; the compact disclosure defaults are checked separately.
  for (const title of ["Web search", "More recommendation controls"]) {
    const summary = screen.getAllByText(title).map((node) => node.closest("summary")).find(Boolean);
    if (summary && !(summary.parentElement as HTMLDetailsElement).open) fireEvent.click(summary);
  }
}

describe("RecommendationsSection", () => {
  beforeEach(() => {
    putSettings.mockClear();
    previewWebPrompt.mockReset();
    previewWebPrompt.mockResolvedValue({ backend: "native", system: "", builtin_guidance: "", inert: false });
  });

  it("keeps common controls and enabled web-search guidance visible", () => {
    renderSection({ "candidates.sources": ["llm_web"], "curator.provider": "none", "llm_web.search_provider": "native" }, false);
    // The cadence, the watched cap and recent releases are always on screen, not behind a disclosure.
    expect(screen.getByText("Titles refresh every")).toBeVisible();
    expect(screen.getByRole("spinbutton", { name: /titles refresh every, in days/i })).toBeVisible();
    expect(screen.getByText("Already-watched titles").closest("details")).toBeNull();
    expect(screen.getByText("More recommendation controls").closest("details")).not.toHaveAttribute("open");
    expect(screen.getByText(/the search runs inside it/i)).toBeVisible();
  });

  // The model is "intent + inline fix": a source's toggle is never disabled; when it's on but its
  // dependency is missing, the card shows exactly how to satisfy it right there.

  it("shows an inline Trakt key field when the Trakt source is on without a key", () => {
    renderSection({ "candidates.sources": ["trakt"] });
    expect(screen.getByLabelText(/Trakt API key/i)).toBeInTheDocument();
  });

  // Whether an AI provider is needed depends on the BACKEND — it used to be asked as one blanket
  // question, which told Exa owners to buy a key they did not need. Exa extracts titles itself.
  it("web search: native with no curator says the search runs inside the AI", () => {
    renderSection({
      "curator.provider": "none",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "native",
    });
    expect(screen.getByText(/the search runs inside it/i)).toBeInTheDocument();
  });

  it("web search: SearXNG with no curator explains WHY it needs one", () => {
    renderSection({
      "curator.provider": "none",
      "searxng.url": "http://searx.local:8080",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "searxng",
    });
    expect(screen.getByText(/returns raw web snippets/i)).toBeInTheDocument();
  });

  it("web search: Exa with no curator does NOT ask for one, because it needs none", () => {
    // The bug this pins: Exa + no AI was reported as needing a key, while the engine ran anyway and
    // billed for every search. Both halves are fixed; this is the UI half.
    renderSection({
      "curator.provider": "none",
      "exa.apikey": "•••••",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "exa",
    });
    expect(screen.queryByText(/needs an AI provider/i)).not.toBeInTheDocument();
  });

  it("AI web search: 'AI provider's own' on a provider that can't self-search (Ollama) warns loudly", () => {
    // Regression: this cell used to show the toggle ON with no prompt while the engine did nothing.
    renderSection({
      "curator.provider": "ollama",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "native",
    });
    expect(
      screen.getByText(/can’t search the web on its own/i),
    ).toBeInTheDocument();
  });

  it("persists an enabled source even when its dependency isn't met yet (intent, not stripped)", async () => {
    renderSection({ "candidates.sources": ["tmdb_similar"] }); // no Trakt key configured
    fireEvent.click(screen.getByLabelText(/Trakt — related titles/i)); // needs a Trakt key
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const sources = putSettings.mock.calls.at(-1)?.[0]?.[
      "candidates.sources"
    ] as string[];
    expect(sources).toContain("trakt"); // kept as intent, NOT stripped for the missing key
  });

  it("AI web search: names the backend and sends you to Connections to change it", () => {
    // The picker and the credential fields moved to the Connections card, so this section must not
    // render a second copy of either — but it still has to say what the source will use.
    renderSection({
      "curator.provider": "ollama",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "searxng",
      "searxng.url": "http://searx.local:8080",
    });
    expect(screen.getByText(/your SearXNG instance/)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Exa$/i })).toBeNull();
    expect(screen.queryByLabelText(/Exa API key/i)).toBeNull();
  });

  it("AI web search: never writes llm_web.search_provider — Connections owns it", async () => {
    // This section PUTs its whole object on any change. While it still held a copy of the backend,
    // saving anything here would overwrite a choice just made in Connections with stale state.
    renderSection({
      "curator.provider": "anthropic",
      "candidates.sources": ["llm_web"],
      "llm_web.search_provider": "searxng",
    });
    fireEvent.click(screen.getByLabelText(/TMDB — discover by taste/i));
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(putSettings.mock.calls.at(-1)?.[0]).not.toHaveProperty(
      "llm_web.search_provider",
    );
  });

  it("persists the owner's intent — enabling a source saves it in candidates.sources", async () => {
    renderSection({ "candidates.sources": ["tmdb_similar"] });
    fireEvent.click(screen.getByLabelText(/TMDB — discover by taste/i));
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const sources = putSettings.mock.calls.at(-1)?.[0]?.[
      "candidates.sources"
    ] as string[];
    expect(sources).toContain("tmdb_discover");
  });

  it("auto-saves a change to the watched cap and carries the sources set too", async () => {
    renderSection({ "recommendations.watched_pct": 0.5 });
    const slider = screen.getByRole("slider", { name: /already-watched/i });
    expect(slider).toHaveValue("50");
    fireEvent.change(slider, { target: { value: "55" } });
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    const body = putSettings.mock.calls.at(-1)?.[0];
    expect(body?.["recommendations.watched_pct"]).toBe(0.55);
    expect(body).toHaveProperty("candidates.sources");
  });

  it("auto-saves the recent-releases weight as a 0..1 fraction", async () => {
    renderSection({ "recommendations.recency": 0.5 });
    const slider = screen.getByRole("slider", { name: /release date counts/i });
    expect(slider).toHaveValue("50");
    fireEvent.change(slider, { target: { value: "75" } });
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(
      putSettings.mock.calls.at(-1)?.[0]?.["recommendations.recency"],
    ).toBe(0.75);
  });

  it("shows a server that chose to turn it off as off, not as the shipped default", () => {
    // The control must render the STORED value, never the default — otherwise the UI advertises
    // ranking the engine is not doing for anyone who deliberately turned it back down.
    renderSection({ "recommendations.recency": 0 });
    expect(
      screen.getByRole("slider", { name: /release date counts/i }),
    ).toHaveValue("0");
  });

  it("shows a fresh install at the shipped default", () => {
    // No stored row: every server, new or upgraded, follows DEFAULTS (0.5).
    renderSection({});
    expect(
      screen.getByRole("slider", { name: /release date counts/i }),
    ).toHaveValue("50");
  });

  it("keeps recent releases and the rebuild cadence as two separate controls", () => {
    // They are near-synonyms in English and completely different settings here. If one ever
    // replaces the other in this card, the owner silently loses a control.
    renderSection({});
    expect(
      screen.getByRole("slider", { name: /release date counts/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("spinbutton", { name: /titles refresh every, in days/i }),
    ).toBeInTheDocument();
  });

  it("saves the idle hold, and says what turning it off means", async () => {
    renderSection({ "recommendations.idle_hold_days": 0 });
    // Off is the shipped default, so the control has to explain the DEFAULT, not just the feature.
    expect(
      screen.getByText(/refresh when due, whatever/i),
    ).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^a month$/i }));

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(
      putSettings.mock.calls.at(-1)?.[0]?.["recommendations.idle_hold_days"],
    ).toBe(30);
  });

  it("says the hold has a ceiling, not that it freezes the row", async () => {
    // The one thing an owner must not misread. A row that stopped for ever would be the opposite of
    // what this is for, and the number is the only thing on screen that says otherwise.
    renderSection({ "recommendations.idle_hold_days": 30 });
    expect(
      screen.getByText(/refreshes anyway after 30 days/i),
    ).toBeInTheDocument();
  });

  it("warns when the hold can never fire because the cadence already beats it", async () => {
    // A row is rebuilt on its due night, so at its next due night its age is exactly the cadence —
    // the hold only bites when it is strictly greater. Both controls offer 14 and 30 as presets, so
    // this is a plausible thing to set and a complete no-op, with nothing on screen saying so.
    renderSection({
      "recommendations.refresh_days": 30,
      "recommendations.idle_hold_days": 30,
    });
    expect(screen.getByText(/no effect/i)).toBeInTheDocument();
  });

  it("warns that a hold does nothing on rows that never rebuild", () => {
    // Cadence 0 is "Never" — a one-click preset. `_is_refresh_night` returns False at 0, so the row
    // never comes due and the hold can never fire. The field said only "rebuilds anyway after 30
    // days, so a row never goes stale", which is false for a row that never rebuilds — and the docs
    // promised all three surfaces warn while only the support endpoint did.
    renderSection({
      "recommendations.refresh_days": 0,
      "recommendations.idle_hold_days": 30,
    });
    expect(screen.getByText(/never refresh/i)).toBeInTheDocument();
  });

  it("does not warn when the hold is above the cadence", () => {
    renderSection({
      "recommendations.refresh_days": 8,
      "recommendations.idle_hold_days": 30,
    });
    expect(screen.queryByText(/no effect/i)).not.toBeInTheDocument();
  });

  it("saves the cold-start choice, and says what it will actually do", async () => {
    renderSection({ "recommendations.cold_start": "popular" });
    const select = screen.getByLabelText(/hasn’t watched enough/i);
    expect(select).toHaveValue("popular");

    fireEvent.change(select, { target: { value: "skip" } });

    // The consequence updates with the choice — this is the line that tells an owner the setting
    // REMOVES a row, which the option label alone never says.
    expect(
      screen.getByText(/any row they already have is removed/i),
    ).toBeInTheDocument();
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(
      putSettings.mock.calls.at(-1)?.[0]?.["recommendations.cold_start"],
    ).toBe("skip");
  });

  it("saves the history threshold the cold-start choice hangs off", async () => {
    renderSection({ "recommendations.min_history": 10 });
    const input = screen.getByLabelText(/Enough watch history/i);
    expect(input).toHaveValue(10);

    fireEvent.change(input, { target: { value: "4" } });
    fireEvent.blur(input);

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(
      putSettings.mock.calls.at(-1)?.[0]?.["recommendations.min_history"],
    ).toBe(4);
  });

  // The web-search count is a SLICE of the seed budget, not a peer of it — `candidates.py` searches
  // `seeds[:recent_count]`. Rendered side by side, the only thing saying so was word order, and the
  // narrower field had to spend a paragraph explaining the field above it. Asserted as containment
  // rather than as copy: the relationship is what the fix is, and copy can be reworded without
  // breaking it.
  it("nests the web-search count inside the seed budget it slices", () => {
    renderSection({
      "recommendations.max_seeds": 40,
      "recommendations.recent_count": 10,
    });

    const budget = screen.getByLabelText(/^How many recent watches to match$/i);
    const slice = screen.getByLabelText(
      /^Watches the AI web search looks up$/i,
    );
    const budgetBlock = budget.closest("div.border-t");

    expect(budgetBlock).not.toBeNull();
    expect(budgetBlock?.contains(slice)).toBe(true);
  });

  it("no longer restates the seed budget under the field that slices it", () => {
    // The old helper opened "A narrower slice of the same list: …" and closed by repeating the
    // AI web search card's own "cached for 7 days" line — 293 characters explaining the control
    // above it, which nesting now says for free.
    renderSection({ "recommendations.recent_count": 10 });

    expect(screen.queryByText(/a narrower slice of the same list/i)).toBeNull();
    expect(screen.queryByText(/cached for 7 days/i)).toBeNull();
  });

  it("shows Shortlist's built-in instructions until the owner writes their own, which starts from its template", async () => {
    previewWebPrompt.mockResolvedValue({
      backend: "exa",
      system: "",
      builtin_guidance: "BUILT IN TEXT",
      builtin_template: "Pick {count} from {last_year} or {year}.",
      inert: false,
    });
    renderSection({ "llm_web.instructions": "", "candidates.sources": ["tmdb_similar", "llm_web"] });
    expect(await screen.findByText("BUILT IN TEXT")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Write your own" }));
    // The template, not the rendered text: a saved copy of "2025 or 2026" would never move on.
    expect(screen.getByLabelText("AI instructions")).toHaveValue("Pick {count} from {last_year} or {year}.");
    expect(screen.getByText("You can use {count}, {year} and {last_year}.")).toBeInTheDocument();
  });

  it("stores the built-in template, untouched, as empty so no row rebuilds", async () => {
    previewWebPrompt.mockResolvedValue({
      backend: "native",
      system: "",
      builtin_guidance: "BUILT IN TEXT",
      builtin_template: "Pick {count} from {year}.",
      inert: false,
    });
    renderSection({ "llm_web.instructions": "", "candidates.sources": ["llm_web"] });
    await userEvent.click(await screen.findByRole("button", { name: "Write your own" }));
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(putSettings.mock.calls.at(-1)?.[0]["llm_web.instructions"]).toBe("");
    // Trailing whitespace is still the template.
    await userEvent.type(screen.getByLabelText("AI instructions"), "  ");
    await waitFor(() => expect(putSettings).toHaveBeenCalledTimes(2));
    expect(putSettings.mock.calls.at(-1)?.[0]["llm_web.instructions"]).toBe("");
  });

  it("stores the template once the owner changes it", async () => {
    previewWebPrompt.mockResolvedValue({
      backend: "native",
      system: "",
      builtin_guidance: "BUILT IN TEXT",
      builtin_template: "Pick {count} from {year}.",
      inert: false,
    });
    renderSection({ "llm_web.instructions": "", "candidates.sources": ["llm_web"] });
    await userEvent.click(await screen.findByRole("button", { name: "Write your own" }));
    await userEvent.type(screen.getByLabelText("AI instructions"), " No horror.");
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith(
        expect.objectContaining({ "llm_web.instructions": "Pick {count} from {year}. No horror." }),
      ),
    );
  });

  it("asks for web search to be turned on, rather than loading forever, when it is off", () => {
    renderSection({ "llm_web.instructions": "", "candidates.sources": ["tmdb_similar"] });
    const message = screen.getByText("Turn on web search to set these.");
    expect(message.parentElement?.querySelector(".animate-pulse")).toBeNull();
    expect(screen.queryByRole("button", { name: "Write your own" })).toBeNull();
  });

  it("autosaves the owner's instructions", async () => {
    renderSection({ "llm_web.instructions": "Favour classics.", "candidates.sources": ["llm_web"] });
    const box = screen.getByLabelText("AI instructions");
    await userEvent.type(box, " Any decade.");
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith(
        expect.objectContaining({ "llm_web.instructions": "Favour classics. Any decade." }),
      ),
    );
  });

  it("Reset to Shortlist's default clears the setting", async () => {
    renderSection({ "llm_web.instructions": "Favour classics.", "candidates.sources": ["llm_web"] });
    await userEvent.click(screen.getByRole("button", { name: "Reset to Shortlist's default" }));
    await waitFor(() =>
      expect(putSettings).toHaveBeenCalledWith(expect.objectContaining({ "llm_web.instructions": "" })),
    );
  });

  it("says so, with a Retry, when the built-in instructions won't load", async () => {
    previewWebPrompt.mockRejectedValue(new Error("boom"));
    renderSection({ "llm_web.instructions": "", "candidates.sources": ["llm_web"] });
    expect(await screen.findByText("Couldn't load Shortlist's default instructions.")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Retry" })).toBeInTheDocument();
  });

  it("keeps the textarea, focused, when the owner clears it", async () => {
    renderSection({ "llm_web.instructions": "Favour classics.", "candidates.sources": ["llm_web"] });
    const box = screen.getByLabelText("AI instructions");
    await userEvent.clear(box);
    const after = screen.getByLabelText("AI instructions");
    expect(after).toBe(box);
    expect(after).toHaveFocus();
    expect(screen.queryByRole("button", { name: "Write your own" })).toBeNull();
    await waitFor(() => expect(putSettings).toHaveBeenCalledWith(expect.objectContaining({ "llm_web.instructions": "" })));
  });

  it("saves whitespace-only instructions as empty", async () => {
    renderSection({ "llm_web.instructions": "Favour classics.", "candidates.sources": ["llm_web"] });
    const box = screen.getByLabelText("AI instructions");
    await userEvent.clear(box);
    await userEvent.type(box, "   ");
    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(putSettings.mock.calls.at(-1)?.[0]["llm_web.instructions"]).toBe("");
  });

  it("Reset returns to the built-in view and saves empty", async () => {
    previewWebPrompt.mockResolvedValue({ backend: "native", system: "", builtin_guidance: "BUILT IN TEXT", inert: false });
    renderSection({ "llm_web.instructions": "Favour classics.", "candidates.sources": ["llm_web"] });
    await userEvent.click(screen.getByRole("button", { name: "Reset to Shortlist's default" }));
    expect(await screen.findByText("BUILT IN TEXT")).toBeInTheDocument();
    expect(screen.queryByRole("textbox", { name: "AI instructions" })).toBeNull();
    await waitFor(() => expect(putSettings).toHaveBeenCalledWith(expect.objectContaining({ "llm_web.instructions": "" })));
  });

  it("does not fetch the built-in instructions while web search is off", () => {
    renderSection({ "llm_web.instructions": "", "candidates.sources": ["tmdb_similar"] });
    expect(previewWebPrompt).not.toHaveBeenCalled();
  });
});
