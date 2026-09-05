/**
 * `step-customize` must not overwrite settings the owner has already saved.
 *
 * The wizard swaps the step COMPONENT per step, so going Back and forward again remounts this one
 * from scratch. Its three fields were plain `useState` literals and it never read settings at all,
 * so the remount silently reset them to defaults — and then Save wrote those defaults over whatever
 * was stored. The worst case is the "Skip for now" button, which is also a save: a control that
 * promises nothing changes was replacing a custom row name with the classic default.
 */
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { StepCustomize } from "@/pages/setup/step-customize";

const { getSettings, putSettings } = vi.hoisted(() => ({
  getSettings: vi.fn(),
  putSettings: vi.fn((_values: Record<string, unknown>) => Promise.resolve({})),
}));

vi.mock("@/lib/api", () => ({
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
  api: {
    getSettings: () => getSettings(),
    putSettings: (values: Record<string, unknown>) => putSettings(values),
  },
}));

const SAVED = {
  "row.name_template": "🔥 My Own Row Name",
  "row.size": 25,
};

function renderStep() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  render(
    <QueryClientProvider client={client}>
      <StepCustomize
        data={{}}
        update={vi.fn()}
        next={vi.fn()}
        complete={vi.fn()}
      />
    </QueryClientProvider>,
  );
}

/** The values sent by the first save. Extracted so strict mode is satisfied once, not four times. */
function firstSave(): Record<string, unknown> {
  const call = putSettings.mock.calls[0];
  if (!call) throw new Error("putSettings was never called");
  return call[0];
}

beforeEach(() => {
  vi.clearAllMocks();
  getSettings.mockResolvedValue(SAVED);
});

describe("StepCustomize seeds from what is already saved", () => {
  it("does not overwrite a custom row name when the owner presses Save", async () => {
    renderStep();
    await waitFor(() => expect(getSettings).toHaveBeenCalled());

    await userEvent.click(
      await screen.findByRole("button", { name: /^save/i }),
    );

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(firstSave()).toMatchObject({
      "row.name_template": "🔥 My Own Row Name",
      "row.size": 25,
    });
  });

  it("does not overwrite a custom row name via Skip either", async () => {
    // "Skip for now — you can change this later" is the same mutation. A control whose label
    // promises nothing changes must not be the one that clobbers the row name.
    renderStep();
    await waitFor(() => expect(getSettings).toHaveBeenCalled());

    await userEvent.click(
      await screen.findByRole("button", { name: /skip for now/i }),
    );

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(firstSave()).toMatchObject({
      "row.name_template": "🔥 My Own Row Name",
      "row.size": 25,
    });
  });

  it("still saves the defaults on a fresh install with nothing stored", async () => {
    getSettings.mockResolvedValue({});
    renderStep();
    await waitFor(() => expect(getSettings).toHaveBeenCalled());

    await userEvent.click(
      await screen.findByRole("button", { name: /^save/i }),
    );

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(firstSave()["row.name_template"]).toContain(
      "Picked for You",
    );
  });

  it("does not clobber a choice the owner made before settings arrived", async () => {
    // Functional updaters, same as step-history: a slow fetch must never win against typing.
    let resolve!: (value: Record<string, unknown>) => void;
    getSettings.mockReturnValue(
      new Promise<Record<string, unknown>>((r) => {
        resolve = r;
      }),
    );
    renderStep();

    // The dynamic option renders its template with sample values ("Because you watched Fargo"),
    // so match on the hint, which is stable copy.
    await userEvent.click(await screen.findByText(/renamed each night/i));
    resolve(SAVED);

    await waitFor(() => expect(getSettings).toHaveBeenCalled());
    await userEvent.click(
      await screen.findByRole("button", { name: /^save/i }),
    );

    await waitFor(() => expect(putSettings).toHaveBeenCalled());
    expect(firstSave()["row.name_template"]).toContain(
      "{top_seed}",
    );
  });
});
