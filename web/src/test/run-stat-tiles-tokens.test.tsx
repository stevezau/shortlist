/**
 * The AI TOKENS tile has to keep saying where the tokens went.
 *
 * Splitting the total into input and output (06178b83) replaced the hint outright, so a run that
 * recorded both the split and the per-step breakdown showed only "N in · M out" — the breakdown the
 * tile had shown before was dropped without anyone asking for it to go.
 */
import { render, screen, within } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RunStatTiles } from "@/components/runs/run-stat-tiles";
import { makeTilesRun } from "@/test/run-fixtures";

function renderTokenTile(stats: Record<string, unknown>): HTMLElement {
  render(<RunStatTiles run={makeTilesRun({}, stats)} />);
  const tile = screen.getByText("AI tokens").closest<HTMLElement>("[title]");
  if (!tile) throw new Error("the AI tokens tile did not render");
  return tile;
}

describe("the AI TOKENS tile", () => {
  // Matched on the tile's text rather than one element's: each part is its own element, so the tile
  // can wrap between parts before it wraps inside one.
  it("shows input and output AND the per-step breakdown when the run recorded both", () => {
    const tile = renderTokenTile({
      llm_tokens: 520088,
      llm_output_tokens: 20088,
      llm_tokens_by_step: { curate: 52625, llm_web: 467463 },
    });

    expect(tile).toHaveTextContent("500,000 in · 20,088 out");
    expect(tile).toHaveTextContent("web search 467,463 · final picks 52,625");
    expect(tile).not.toHaveTextContent("sent + received");
  });

  it("does not repeat the total as a one-step breakdown", () => {
    const tile = renderTokenTile({
      llm_tokens: 360939,
      llm_output_tokens: 90782,
      llm_tokens_by_step: { llm_web: 360939 },
    });

    expect(tile).toHaveTextContent("270,157 in · 90,782 out");
    expect(tile).not.toHaveTextContent("web search");
  });

  it("shows only input and output when the run recorded the split but no steps", () => {
    const tile = renderTokenTile({ llm_tokens: 520088, llm_output_tokens: 20088 });

    expect(tile).toHaveTextContent("500,000 in · 20,088 out");
    expect(tile).not.toHaveTextContent(/web search|final picks|sent \+ received/);
  });

  it("keeps the older wording for a run recorded before the split, steps included", () => {
    const tile = renderTokenTile({
      llm_tokens: 520088,
      llm_tokens_by_step: { curate: 52625, llm_web: 467463 },
    });

    expect(
      within(tile).getByText("web search 467,463 · final picks 52,625 · sent + received"),
    ).toBeInTheDocument();
    expect(within(tile).queryByText(/ in · /)).not.toBeInTheDocument();
  });

  it("says only sent + received for a run recorded before either was measured", () => {
    const tile = renderTokenTile({ llm_tokens: 520088 });

    expect(within(tile).getByText("sent + received")).toBeInTheDocument();
    expect(within(tile).queryByText(/web search|final picks| in · /)).not.toBeInTheDocument();
  });
});
