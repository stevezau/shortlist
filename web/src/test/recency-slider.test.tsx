import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RecencySlider } from "@/components/settings/recency-slider";

const THIS_YEAR = new Date().getFullYear();

describe("RecencySlider", () => {
  it("reports the value on a real slider so it is keyboard-reachable", () => {
    render(<RecencySlider value={40} onChange={() => {}} />);
    const slider = screen.getByRole("slider", { name: /release date counts/i });
    expect(slider).toHaveValue("40");
    expect(slider).toHaveAttribute(
      "aria-valuetext",
      "40 percent towards recent releases",
    );
  });

  it("hands the new whole percent back on drag", () => {
    const onChange = vi.fn();
    render(<RecencySlider value={50} onChange={onChange} />);

    fireEvent.change(
      screen.getByRole("slider", { name: /release date counts/i }),
      { target: { value: "55" } },
    );

    expect(onChange).toHaveBeenCalledWith(55);
  });

  it("labels the era bars with years counted back from today, not hardcoded ones", () => {
    render(<RecencySlider value={60} onChange={() => {}} />);
    for (const age of [0, 10, 20, 30, 40]) {
      expect(screen.getByText(String(THIS_YEAR - age)).textContent).toBe(
        String(THIS_YEAR - age),
      );
    }
  });

  it("says release date is ignored when the slider is at zero", () => {
    render(<RecencySlider value={0} onChange={() => {}} />);
    expect(screen.getByText(/release date is ignored/i)).toBeInTheDocument();
  });

  it("states the trade-off in years once it is turned up", () => {
    render(<RecencySlider value={100} onChange={() => {}} />);
    // Full strength = the 8-year half-life, so the sentence must name this year minus 8.
    expect(
      screen.getByText(new RegExp(`A ${THIS_YEAR - 8} title ranks about half`)),
    ).toBeInTheDocument();
  });

  it("keeps the bars out of the accessibility tree, since the sentence already says it", () => {
    // Otherwise a screen reader reads ten bare numbers ("2026 100% 2016 42%…") before the sentence
    // that explains them.
    const { container } = render(
      <RecencySlider value={50} onChange={() => {}} />,
    );
    expect(container.querySelector('[aria-hidden="true"]')).not.toBeNull();
  });
});
