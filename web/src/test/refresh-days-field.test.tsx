import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { RefreshDaysField } from "@/components/settings/refresh-days-field";

/** Drives the controlled field the way Settings and the row editor do. */
function Harness({
  start,
  onCommit,
}: {
  start: number;
  onCommit: (n: number) => void;
}) {
  const [days, setDays] = useState(start);
  return (
    <RefreshDaysField
      value={days}
      onChange={(next) => {
        setDays(next);
        onCommit(next);
      }}
    />
  );
}

const box = () =>
  screen.getByRole("spinbutton", { name: /how often the row rebuilds/i });

describe("RefreshDaysField", () => {
  it("says the cadence in plain days, which is the whole point of the control", () => {
    render(<Harness start={8} onCommit={vi.fn()} />);

    expect(box()).toHaveValue(8);
    // The percentage this replaced needed a sentence to translate itself ("55%… about every 7 days").
    expect(screen.getByText(/Rebuilds every 8 days/i)).toBeInTheDocument();
  });

  it("names the two ends rather than reporting a bare number", () => {
    const { rerender } = render(
      <RefreshDaysField value={0} onChange={vi.fn()} />,
    );
    expect(screen.getByText(/Frozen/i)).toBeInTheDocument();

    rerender(<RefreshDaysField value={1} onChange={vi.fn()} />);
    expect(screen.getByText(/every night/i)).toBeInTheDocument();
  });

  it("commits a typed cadence on blur", async () => {
    const onCommit = vi.fn();
    render(<Harness start={8} onCommit={onCommit} />);

    await userEvent.clear(box());
    await userEvent.type(box(), "21");
    await userEvent.tab();

    expect(onCommit).toHaveBeenCalledWith(21);
  });

  it("clamps out-of-range input instead of sending it to an API that would 422", async () => {
    const onCommit = vi.fn();
    render(<Harness start={8} onCommit={onCommit} />);

    await userEvent.clear(box());
    await userEvent.type(box(), "9999");
    await userEvent.tab();

    expect(onCommit).toHaveBeenCalledWith(365);
  });

  it("keeps the old value when the box is cleared and left empty", async () => {
    const onCommit = vi.fn();
    render(<Harness start={8} onCommit={onCommit} />);

    await userEvent.clear(box());
    await userEvent.tab();

    expect(onCommit).not.toHaveBeenCalled();
    expect(box()).toHaveValue(8);
  });

  it("offers the cadences people actually want as one click", async () => {
    const onCommit = vi.fn();
    render(<Harness start={8} onCommit={onCommit} />);

    // Monthly is the case the old 0..1 fraction could not express AT ALL — it bottomed out at a
    // fortnight — so this is the button that proves the change did something.
    await userEvent.click(screen.getByRole("button", { name: "Monthly" }));

    expect(onCommit).toHaveBeenCalledWith(30);
    expect(box()).toHaveValue(30);
  });

  it("re-syncs its text buffer when the value changes from outside", () => {
    /**
     * The row editor's inherit toggle seeds the global into this field, and the presets set it from
     * outside the input. The buffer is adjusted DURING RENDER rather than in an effect (the pattern
     * `row-size-field` uses): an effect paints the stale number for a frame first, and ESLint's
     * `react-hooks/set-state-in-effect` rejects it outright — which is how CI caught it.
     */
    const { rerender } = render(
      <RefreshDaysField value={8} onChange={vi.fn()} />,
    );
    expect(box()).toHaveValue(8);

    rerender(<RefreshDaysField value={30} onChange={vi.fn()} />);

    expect(box()).toHaveValue(30);
    expect(screen.getByText(/Rebuilds every 30 days/i)).toBeInTheDocument();
  });
});
