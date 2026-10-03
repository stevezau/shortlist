import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { type ReactNode, useState } from "react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { RowAiInstructionsField } from "@/components/rows/row-ai-instructions-field";
import type { AiInstructionsInert } from "@/lib/sources";
import type { AiInstructions, Settings } from "@/lib/types";

const { getSettings, previewWebPrompt } = vi.hoisted(() => ({
  getSettings: vi.fn(),
  previewWebPrompt: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: { getSettings: () => getSettings(), previewWebPrompt: (body: unknown) => previewWebPrompt(body) },
}));

type Props = {
  value: AiInstructions;
  onChange?: (next: AiInstructions) => void;
  otherSources?: string[];
  backend?: string;
  inert?: AiInstructionsInert;
};

function wrap(children: ReactNode) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return (
    <QueryClientProvider client={client}>
      <MemoryRouter>{children}</MemoryRouter>
    </QueryClientProvider>
  );
}

function renderField({ value, onChange = vi.fn(), otherSources = [], backend = "native", inert = null }: Props) {
  render(
    wrap(
      <RowAiInstructionsField
        value={value}
        onChange={onChange}
        otherSources={otherSources}
        backend={backend}
        inert={inert}
      />,
    ),
  );
}

/** A parent that holds the value, as the row editor's draft does. */
function renderLive(initial: AiInstructions, onChange: (next: AiInstructions) => void) {
  function Harness() {
    const [value, setValue] = useState(initial);
    return (
      <RowAiInstructionsField
        value={value}
        onChange={(next) => {
          onChange(next);
          setValue(next);
        }}
        otherSources={[]}
        backend="native"
        inert={null}
      />
    );
  }
  render(wrap(<Harness />));
}

describe("RowAiInstructionsField", () => {
  beforeEach(() => {
    getSettings.mockReset();
    getSettings.mockResolvedValue({} as Settings);
    previewWebPrompt.mockReset();
    previewWebPrompt.mockResolvedValue({ backend: "native", system: "", builtin_guidance: "BUILT IN", inert: false });
  });

  it("shows the default text and no textarea when the row uses the default", async () => {
    renderField({ value: { mode: "default", text: "" } });
    expect(screen.getByRole("button", { name: "Use the default" })).toHaveAttribute("aria-pressed", "true");
    expect(screen.queryByRole("textbox")).toBeNull();
    expect(await screen.findByText("BUILT IN")).toBeInTheDocument();
    expect(previewWebPrompt).toHaveBeenCalledWith({});
  });

  it("shows the server-wide instructions as the default when the owner has written some", async () => {
    getSettings.mockResolvedValue({ "llm_web.instructions": "Favour classics." } as Settings);
    renderField({ value: { mode: "default", text: "" } });
    expect(await screen.findByText("Favour classics.")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Settings → Defaults → Title sources" })).toHaveAttribute(
      "href",
      "/settings/defaults#sources",
    );
    expect(previewWebPrompt).not.toHaveBeenCalled();
  });

  it("switching to Add to the default reveals a 2000-character textarea and reports the change", async () => {
    const onChange = vi.fn();
    renderLive({ mode: "default", text: "" }, onChange);
    await userEvent.click(screen.getByRole("button", { name: "Add to the default" }));
    expect(onChange).toHaveBeenCalledWith({ mode: "add", text: "" });
    expect(screen.getByRole("textbox", { name: "Also tell the AI" })).toHaveAttribute("maxlength", "2000");
    expect(screen.getByText("Added after the default instructions, for this row only.")).toBeInTheDocument();
    expect(screen.getByText("You can use {count}, {year} and {last_year}.")).toBeInTheDocument();
  });

  it("writing your own offers the placeholders, a reset, and what Shortlist always adds", async () => {
    const onChange = vi.fn();
    renderField({ value: { mode: "own", text: "Any decade." }, onChange });
    expect(screen.getByRole("textbox", { name: "Your instructions" })).toHaveAttribute("maxlength", "2000");
    expect(screen.getByText("You can use {count}, {year} and {last_year}.")).toBeInTheDocument();
    expect(screen.getByText("Shortlist always adds these")).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reset to the default" }));
    expect(onChange).toHaveBeenCalledWith({ mode: "default", text: "Any decade." });
  });

  it("warns that a row with its own instructions may make its own AI call", () => {
    renderField({ value: { mode: "add", text: "x" } });
    expect(screen.getByText(/can't share one AI web search/)).toBeInTheDocument();
  });

  it("names the sources that won't read the instructions", () => {
    renderField({ value: { mode: "add", text: "x" }, otherSources: ["TMDB similar", "Trakt"] });
    expect(screen.getByText(/TMDB similar and Trakt don't read these instructions/)).toBeInTheDocument();
  });

  it("says doesn't when only one other source won't read the instructions", () => {
    renderField({ value: { mode: "add", text: "x" }, otherSources: ["TMDB similar"] });
    expect(
      screen.getByText("TMDB similar doesn't read these instructions, so this row will be a mix."),
    ).toBeInTheDocument();
  });

  it("asks for the instructions when a row adds to or replaces the default with nothing written", () => {
    const message = "Write the instructions, or choose Use the default.";
    renderField({ value: { mode: "add", text: "" } });
    expect(screen.getByText(message)).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "Also tell the AI" })).toHaveAccessibleDescription(
      expect.stringContaining(message),
    );
    cleanup();
    renderField({ value: { mode: "own", text: "   " } });
    expect(screen.getByText(message)).toBeInTheDocument();
    cleanup();
    renderField({ value: { mode: "add", text: "x" } });
    expect(screen.queryByText(message)).toBeNull();
    cleanup();
    renderField({ value: { mode: "default", text: "" } });
    expect(screen.queryByText(message)).toBeNull();
  });

  it("lists three or more sources with commas and a final and", () => {
    renderField({ value: { mode: "add", text: "x" }, otherSources: ["TMDB similar", "TMDB discover", "Trakt"] });
    expect(
      screen.getByText("TMDB similar, TMDB discover and Trakt don't read these instructions, so this row will be a mix."),
    ).toBeInTheDocument();
  });

  it("says the instructions do nothing on Exa without an AI provider", () => {
    renderField({ value: { mode: "add", text: "x" }, backend: "exa", inert: "no_provider" });
    expect(
      screen.getByText(
        "With no AI provider, Exa's titles are used as found, so these instructions have no effect. Add an AI provider in Settings → Connections.",
      ),
    ).toBeInTheDocument();
  });

  it("says the instructions do nothing yet on any other backend without an AI provider", () => {
    renderField({ value: { mode: "add", text: "x" }, backend: "native", inert: "no_provider" });
    expect(
      screen.getByText(
        "AI web search needs an AI provider, so these instructions have no effect yet. Add one in Settings → Connections.",
      ),
    ).toBeInTheDocument();
  });

  it("says the instructions do nothing on native search when the AI provider can't search for itself", () => {
    renderField({ value: { mode: "add", text: "x" }, backend: "native", inert: "no_native_search" });
    expect(
      screen.getByText(
        "Your AI provider can't search the web itself, so these instructions have no effect. Choose Exa or SearXNG in Settings → Connections.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText(/needs an AI provider/)).toBeNull();
  });

  it("explains what the instructions steer on Exa and SearXNG, and says nothing extra on native", () => {
    renderField({ value: { mode: "add", text: "x" }, backend: "exa" });
    expect(screen.getByText(/these instructions decide which of Exa's titles the AI keeps/)).toBeInTheDocument();
    cleanup();
    renderField({ value: { mode: "add", text: "x" }, backend: "searxng" });
    expect(screen.getByText(/You search with SearXNG/)).toBeInTheDocument();
    cleanup();
    renderField({ value: { mode: "add", text: "x" }, backend: "native" });
    expect(screen.queryByText(/You search with/)).toBeNull();
    expect(screen.queryByText(/have no effect/)).toBeNull();
  });

  it("shows exactly what's sent when opened", async () => {
    previewWebPrompt.mockResolvedValue({ backend: "native", system: "SYSTEM TEXT", builtin_guidance: "", inert: false });
    renderField({ value: { mode: "add", text: "x" } });
    expect(previewWebPrompt).not.toHaveBeenCalled();
    await userEvent.click(screen.getByText("Exactly what's sent"));
    expect(await screen.findByText(/SYSTEM TEXT/)).toBeInTheDocument();
    expect(previewWebPrompt).toHaveBeenCalledWith({ ai_instructions: { mode: "add", text: "x" } });
    expect(screen.getByText("Then the person's 20 most recent watches are added.")).toBeInTheDocument();
  });

  it.each([
    ["exa", "Then the person's 20 most recent watches and the titles Exa found are added."],
    ["searxng", "Then the person's 20 most recent watches and excerpts from the articles found are added."],
  ])("says what else is sent on %s", async (backend, footnote) => {
    previewWebPrompt.mockResolvedValue({ backend, system: "SYSTEM TEXT", builtin_guidance: "", inert: false });
    renderField({ value: { mode: "add", text: "x" }, backend });
    await userEvent.click(screen.getByText("Exactly what's sent"));
    expect(await screen.findByText(footnote)).toBeInTheDocument();
  });

  it("offers a retry when the preview can't load", async () => {
    previewWebPrompt.mockRejectedValueOnce(new Error("boom"));
    renderField({ value: { mode: "add", text: "x" } });
    await userEvent.click(screen.getByText("Exactly what's sent"));
    expect(await screen.findByText("Couldn't load the preview.")).toBeInTheDocument();
    previewWebPrompt.mockResolvedValue({ backend: "native", system: "SYSTEM TEXT", builtin_guidance: "", inert: false });
    await userEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(await screen.findByText(/SYSTEM TEXT/)).toBeInTheDocument();
  });
});
