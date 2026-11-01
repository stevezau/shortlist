import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RecencySlider } from "@/components/settings/recency-slider";

const THIS_YEAR = new Date().getFullYear();

describe("RecencySlider", () => {
  it("names the newest bar instead of printing a 100% that can never move", () => {
    // Every bar is measured against a title released THIS YEAR, so the leftmost one is full at every
    // slider position. Labelling a fixed reference "100%" makes it look like a value that ought to
    // respond to the control — which is exactly how it was read.
    const { rerender } = render(
      <RecencySlider value={65} onChange={() => {}} />,
    );
    expect(screen.getByText("this year")).toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();

    // Still true at the other end of the range — the point is that it NEVER moves.
    rerender(<RecencySlider value={5} onChange={() => {}} />);
    expect(screen.getByText("this year")).toBeInTheDocument();
    expect(screen.queryByText("100%")).not.toBeInTheDocument();
  });

  it("still prints a real percentage on the older bars, which do move", () => {
    const { rerender } = render(
      <RecencySlider value={90} onChange={() => {}} />,
    );
    const strong = screen.getAllByText(/^\d+%$/).map((n) => n.textContent);
    rerender(<RecencySlider value={10} onChange={() => {}} />);
    const weak = screen.getAllByText(/^\d+%$/).map((n) => n.textContent);
    expect(strong).not.toEqual(weak);
  });

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
      expect(screen.getByText(String(THIS_YEAR - age))).toBeInTheDocument();
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

  describe("older side (#174)", () => {
    it("spans -100..100 with the ends named", () => {
      render(<RecencySlider value={0} onChange={() => {}} />);
      const slider = screen.getByRole("slider", { name: /release date counts/i });
      expect(slider).toHaveAttribute("min", "-100");
      expect(slider).toHaveAttribute("max", "100");
      expect(screen.getByText("Older")).toBeInTheDocument();
      expect(screen.getByText("Ignore age")).toBeInTheDocument();
      expect(screen.getByText("Newer")).toBeInTheDocument();
    });

    it("hands a negative percent back on drag", () => {
      const onChange = vi.fn();
      render(<RecencySlider value={0} onChange={onChange} />);
      fireEvent.change(
        screen.getByRole("slider", { name: /release date counts/i }),
        { target: { value: "-35" } },
      );
      expect(onChange).toHaveBeenCalledWith(-35);
    });

    it("describes the lean in plain words, as a nudge and not a filter", () => {
      render(<RecencySlider value={-100} onChange={() => {}} />);
      expect(screen.getByText(/leans older/i)).toHaveTextContent(
        /40-year-old title scores 32% higher/i,
      );
      expect(screen.getByText(/not a filter/i)).toBeInTheDocument();
      expect(
        screen.getByRole("slider", { name: /release date counts/i }),
      ).toHaveAttribute("aria-valuetext", "100 percent towards older releases");
    });

    it("shows a negative value as how far it leans older, never as a minus", () => {
      render(<RecencySlider value={-40} onChange={() => {}} />);
      expect(screen.getByText("40% older")).toBeInTheDocument();
      expect(screen.queryByText("-40%")).not.toBeInTheDocument();
    });

    it("labels the older bars as boosts, keeping the reference bar named", () => {
      render(<RecencySlider value={-100} onChange={() => {}} />);
      expect(screen.getByText("this year")).toBeInTheDocument();
      expect(screen.getByText("+32%")).toBeInTheDocument();
      expect(screen.getByText("+11%")).toBeInTheDocument();
    });
  });
});
