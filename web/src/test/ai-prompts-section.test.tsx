import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState, type ReactNode } from "react";
import { MemoryRouter } from "react-router";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { AiPromptsSection } from "@/components/rows/ai-prompts-section";
import { ApiError } from "@/lib/api";
import type * as ApiModule from "@/lib/api";
import { blankInput } from "@/lib/collections";
import type { CollectionInput } from "@/lib/types";

const api = vi.hoisted(() => ({ getThemePrompts: vi.fn(), getThemeCapabilities: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, ...api } };
});

const set = vi.fn();

function Harness({ initial }: { initial: CollectionInput }) {
  const [input, setInput] = useState(initial);
  return (
    <AiPromptsSection
      input={input}
      set={(patch) => {
        set(patch);
        setInput((prev) => ({ ...prev, ...patch }));
      }}
    />
  );
}

function renderSection(initial: CollectionInput = blankInput()) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  function Wrapper({ children }: { children: ReactNode }) {
    return (
      <MemoryRouter>
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      </MemoryRouter>
    );
  }
  return render(<Harness initial={initial} />, { wrapper: Wrapper });
}

beforeEach(() => {
  set.mockReset();
  api.getThemePrompts.mockReset();
  api.getThemeCapabilities.mockReset();
  api.getThemePrompts.mockResolvedValue({ guidance: "You curate themed rows.", mechanics: "Reply with JSON only." });
  api.getThemeCapabilities.mockResolvedValue({ ai: true });
});

describe("AiPromptsSection", () => {
  it("shows a skeleton while the prompt loads", () => {
    api.getThemePrompts.mockReturnValue(new Promise(() => undefined));
    renderSection();

    expect(screen.getByRole("status", { name: /loading/i })).toBeInTheDocument();
  });

  it("says what went wrong and retries", async () => {
    api.getThemePrompts.mockRejectedValueOnce(new ApiError(503, "Shortlist didn't answer."));
    renderSection();

    expect(await screen.findByRole("alert")).toHaveTextContent("Shortlist didn't answer.");
    await userEvent.click(screen.getByRole("button", { name: /try again/i }));

    expect(await screen.findByText("You curate themed rows.")).toBeInTheDocument();
  });

  it("shows the default guidance and the locked mechanics, which cannot be edited", async () => {
    renderSection();

    expect(await screen.findByText("You curate themed rows.")).toBeInTheDocument();
    const mechanics = screen.getByText("Reply with JSON only.");
    expect(mechanics.closest("pre")).not.toBeNull();
    expect(screen.getByText(/shortlist always adds this/i)).toBeInTheDocument();
    expect(screen.queryByRole("textbox")).not.toBeInTheDocument();
  });

  it("shows exactly what is sent: the guidance, then the mechanics", async () => {
    renderSection({ ...blankInput(), ai_instructions: { mode: "own", text: "Only films before 2000." } });

    const sent = await screen.findByLabelText("Exactly what’s sent");
    expect(sent).toHaveTextContent("Only films before 2000. Reply with JSON only.");
  });

  it("saves the owner's own guidance with the row", async () => {
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "Write your own" }));

    await userEvent.type(screen.getByLabelText("Your guidance"), "Prefer the 90s.");

    expect(set).toHaveBeenLastCalledWith({ ai_instructions: { mode: "own", text: "Prefer the 90s." } });
    expect(screen.getByLabelText("Exactly what’s sent")).toHaveTextContent("Prefer the 90s. Reply with JSON only.");
  });

  it("adds to the default instead of replacing it", async () => {
    renderSection();
    await userEvent.click(await screen.findByRole("button", { name: "Add to the default" }));
    await userEvent.type(screen.getByLabelText("Also tell the AI"), "No horror.");

    expect(screen.getByLabelText("Exactly what’s sent")).toHaveTextContent(
      "You curate themed rows. No horror. Reply with JSON only.",
    );
  });

  it("asks for words when a mode needs them, and resets to the default on request", async () => {
    renderSection({ ...blankInput(), ai_instructions: { mode: "own", text: "" } });

    expect(await screen.findByText(/write your guidance, or choose use the default/i)).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: "Reset to the default" }));

    expect(set).toHaveBeenLastCalledWith({ ai_instructions: { mode: "default", text: "" } });
    expect(screen.queryByLabelText("Your guidance")).not.toBeInTheDocument();
  });

  it("says the prompt only matters with an AI provider, and fetches none without one", async () => {
    api.getThemeCapabilities.mockResolvedValue({ ai: false });
    renderSection();

    expect(await screen.findByText(/only used when an AI provider is set/i)).toBeInTheDocument();
    expect(api.getThemePrompts).not.toHaveBeenCalled();
  });
});
