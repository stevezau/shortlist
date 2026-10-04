import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { DaysInput, OverTimeFields } from "@/components/rows/over-time-fields";
import type * as ApiModule from "@/lib/api";
import { blankInput } from "@/lib/collections";
import type { Collection, CollectionInput } from "@/lib/types";

const api = vi.hoisted(() => ({ listCollections: vi.fn() }));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return { ...actual, api: { ...actual.api, ...api } };
});

function row(slug: string, patch: Partial<Collection> = {}): Collection {
  return { id: 1, slug, name: `Row ${slug}`, build: "per_person", ...patch } as unknown as Collection;
}

const changes = vi.fn();

function Harness({ ownSlug = "mine", start = blankInput() }: { ownSlug?: string | null; start?: CollectionInput }) {
  const [input, setInput] = useState(start);
  return (
    <OverTimeFields
      input={input}
      ownSlug={ownSlug}
      onChange={(patch) => {
        changes(patch);
        setInput((prev) => ({ ...prev, ...patch }));
      }}
    />
  );
}

function renderFields(props: Parameters<typeof Harness>[0] = {}) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <Harness {...props} />
    </QueryClientProvider>,
  );
}

describe("OverTimeFields", () => {
  it("maps the four options to their shares", async () => {
    api.listCollections.mockResolvedValue([]);
    renderFields();
    const select = await screen.findByLabelText("How much changes each time");

    expect(Array.from((select as HTMLSelectElement).options).map((o) => [o.text, o.value])).toEqual([
      ["A little (about a fifth)", "0.2"],
      ["A third (usual)", ""],
      ["Half", "0.5"],
      ["Almost everything", "0.8"],
    ]);
    await userEvent.selectOptions(select, "Half");
    expect(changes).toHaveBeenLastCalledWith({ refresh_share: 0.5 });
    await userEvent.selectOptions(select, "A third (usual)");
    expect(changes).toHaveBeenLastCalledWith({ refresh_share: null });
  });

  it("hides the day count until the cooldown is switched on, and sends null when it is switched off", async () => {
    api.listCollections.mockResolvedValue([]);
    renderFields();
    const toggle = await screen.findByRole("switch", { name: /Don.t repeat a title/ });

    expect(screen.queryByLabelText("Days before a title can repeat")).toBeNull();
    await userEvent.click(toggle);
    expect(changes).toHaveBeenLastCalledWith({ repeat_cooldown_days: 30 });
    const days = await screen.findByLabelText("Days before a title can repeat");
    await userEvent.clear(days);
    await userEvent.type(days, "90");
    expect(changes).toHaveBeenLastCalledWith({ repeat_cooldown_days: 90 });
    await userEvent.click(toggle);
    expect(changes).toHaveBeenLastCalledWith({ repeat_cooldown_days: null });
    expect(screen.queryByLabelText("Days before a title can repeat")).toBeNull();
  });

  it("lists only the other per-person rows to keep titles out of", async () => {
    api.listCollections.mockResolvedValue([
      row("mine"),
      row("because-you-watched"),
      row("popular", { build: "shared" }),
    ]);
    renderFields();

    expect(await screen.findByRole("checkbox", { name: "Row because-you-watched" })).toBeInTheDocument();
    expect(screen.queryByRole("checkbox", { name: "Row mine" })).toBeNull();
    expect(screen.queryByRole("checkbox", { name: "Row popular" })).toBeNull();
  });

  it("adds and removes a row slug, and sends null when the last is unticked", async () => {
    api.listCollections.mockResolvedValue([row("a"), row("b")]);
    renderFields();

    await userEvent.click(await screen.findByRole("checkbox", { name: "Row a" }));
    expect(changes).toHaveBeenLastCalledWith({ avoid_rows: ["a"] });
    await userEvent.click(screen.getByRole("checkbox", { name: "Row b" }));
    expect(changes).toHaveBeenLastCalledWith({ avoid_rows: ["a", "b"] });
    await userEvent.click(screen.getByRole("checkbox", { name: "Row a" }));
    await userEvent.click(screen.getByRole("checkbox", { name: "Row b" }));
    expect(changes).toHaveBeenLastCalledWith({ avoid_rows: null });
  });

  it("names an AI row by its slug, not by its theme placeholders", async () => {
    api.listCollections.mockResolvedValue([row("ai-twists", { name: "{theme_emoji} {theme}" })]);
    renderFields();

    expect(await screen.findByRole("checkbox", { name: "AI row (ai-twists)" })).toBeInTheDocument();
  });

  it("shows placeholders as grey chips with friendly words, never as raw braces", async () => {
    api.listCollections.mockResolvedValue([
      row("picked", { name: "{library_name} Picked for You" }),
      row("because", { name: "Because you watched {top_seed}" }),
    ]);
    renderFields();

    const picked = await screen.findByRole("checkbox", { name: /Picked for You/ });
    const because = screen.getByRole("checkbox", { name: /Because you watched/ });
    const pickedLabel = picked.closest("label") as HTMLElement;
    const becauseLabel = because.closest("label") as HTMLElement;
    expect(pickedLabel).toHaveTextContent("library name Picked for You");
    expect(becauseLabel).toHaveTextContent("Because you watched top seed");
    expect(within(pickedLabel).getByText("library name")).toHaveClass("bg-muted");
    expect(within(becauseLabel).getByText("top seed")).toHaveClass("bg-muted");
    expect(screen.queryByText(/[{}]/)).not.toBeInTheDocument();
  });

  it("says so when there is no other row to compare with", async () => {
    api.listCollections.mockResolvedValue([row("mine")]);
    renderFields();

    expect(await screen.findByText("No other rows to compare with.")).toBeInTheDocument();
  });

  it("shows an error with a retry when the rows can't be read", async () => {
    api.listCollections.mockRejectedValueOnce(new Error("boom")).mockResolvedValue([row("a")]);
    renderFields();

    await userEvent.click(await screen.findByRole("button", { name: "Read them again" }));
    await waitFor(() => expect(screen.getByRole("checkbox", { name: "Row a" })).toBeInTheDocument());
  });
});

describe("DaysInput", () => {
  function Outside() {
    const [value, setValue] = useState(7);
    return (
      <>
        <DaysInput id="days" label="Days" value={value} min={1} max={90} onCommit={setValue} />
        <button type="button" onClick={() => setValue(30)}>
          Discard
        </button>
      </>
    );
  }

  it("shows the value again when it is changed from outside", async () => {
    render(<Outside />);
    const days = screen.getByLabelText("Days");
    await userEvent.clear(days);
    await userEvent.type(days, "12");

    await userEvent.click(screen.getByRole("button", { name: "Discard" }));

    expect(days).toHaveValue(30);
  });

  it("keeps what is being typed", async () => {
    render(<Outside />);
    const days = screen.getByLabelText("Days");

    await userEvent.clear(days);
    expect(days).toHaveValue(null);
    await userEvent.type(days, "45");

    expect(days).toHaveValue(45);
  });
});
