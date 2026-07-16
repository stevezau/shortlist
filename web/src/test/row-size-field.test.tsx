import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it } from "vitest";

import { RowSizeField } from "@/components/row-size-field";

/** Drives the controlled field the way the settings sections do, and records the committed values. */
function Harness({
  start,
  onCommit,
}: {
  start: number;
  onCommit: (n: number) => void;
}) {
  const [size, setSize] = useState(start);
  return (
    <RowSizeField
      value={size}
      onChange={(next) => {
        setSize(next);
        onCommit(next);
      }}
    />
  );
}

describe("RowSizeField", () => {
  it("commits a freely typed size on blur", async () => {
    const committed: number[] = [];
    render(<Harness start={15} onCommit={(n) => committed.push(n)} />);
    const input = screen.getByLabelText(/Row size/i);

    await userEvent.clear(input);
    await userEvent.type(input, "33");
    await userEvent.tab();

    expect(committed.at(-1)).toBe(33);
  });

  it("clamps values above the max down to the ceiling", async () => {
    const committed: number[] = [];
    render(<Harness start={15} onCommit={(n) => committed.push(n)} />);
    const input = screen.getByLabelText(/Row size/i);

    await userEvent.clear(input);
    await userEvent.type(input, "9999");
    await userEvent.tab();

    expect(committed.at(-1)).toBe(40);
    expect(input).toHaveValue(40);
  });

  it("restores the last good value when the field is left blank", async () => {
    const committed: number[] = [];
    render(<Harness start={20} onCommit={(n) => committed.push(n)} />);
    const input = screen.getByLabelText(/Row size/i);

    await userEvent.clear(input);
    await userEvent.tab(); // blur with an empty field must not save 0 or NaN

    expect(committed).toHaveLength(0);
    expect(input).toHaveValue(20);
  });
});
