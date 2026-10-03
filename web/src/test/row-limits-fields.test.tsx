import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { RowLimitsFields } from "@/components/rows/row-limits-fields";
import { blankInput } from "@/lib/collections";
import type { CollectionInput } from "@/lib/types";

/** Drives the field the way the row editor does and records every patch it sends. */
function Harness({
  start = {},
  patches,
}: {
  start?: Partial<CollectionInput>;
  patches: Partial<CollectionInput>[];
}) {
  const [input, setInput] = useState<CollectionInput>({ ...blankInput(), ...start });
  return (
    <RowLimitsFields
      input={input}
      set={(patch) => {
        patches.push(patch);
        setInput((prev) => ({ ...prev, ...patch }));
      }}
    />
  );
}

describe("RowLimitsFields", () => {
  it("starts empty and says blank means no limit", () => {
    render(<Harness patches={[]} />);
    expect(screen.getByLabelText("Longest it can run (minutes)")).toHaveValue(null);
    expect(screen.getByLabelText("Lowest rating (out of 10)")).toHaveValue(null);
    expect(screen.getAllByText(/blank means no limit/i).length).toBeGreaterThan(0);
  });

  it("sends a number for a typed value", async () => {
    const patches: Partial<CollectionInput>[] = [];
    render(<Harness patches={patches} />);

    await userEvent.type(screen.getByLabelText("Longest it can run (minutes)"), "120");
    await userEvent.tab();
    await userEvent.type(screen.getByLabelText("Lowest rating (out of 10)"), "7.5");
    await userEvent.tab();

    expect(patches).toContainEqual({ max_runtime: 120 });
    expect(patches).toContainEqual({ min_rating: 7.5 });
  });

  it("sends null when a filled input is cleared", async () => {
    const patches: Partial<CollectionInput>[] = [];
    render(<Harness start={{ max_runtime: 90 }} patches={patches} />);

    await userEvent.clear(screen.getByLabelText("Longest it can run (minutes)"));
    await userEvent.tab();

    expect(patches).toEqual([{ max_runtime: null }]);
  });

  it("sends the released-between years independently", async () => {
    const patches: Partial<CollectionInput>[] = [];
    render(<Harness patches={patches} />);

    await userEvent.type(screen.getByLabelText("Released from year"), "1990");
    await userEvent.tab();
    await userEvent.type(screen.getByLabelText("Released up to year"), "2010");
    await userEvent.tab();

    expect(patches).toContainEqual({ min_year: 1990 });
    expect(patches).toContainEqual({ max_year: 2010 });
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("flags a first year after the last year inline", () => {
    render(<Harness start={{ min_year: 2010, max_year: 1990 }} patches={[]} />);
    expect(screen.getByRole("alert")).toHaveTextContent(/can.t be later than/i);
  });
});
