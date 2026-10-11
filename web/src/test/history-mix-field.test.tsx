import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { HistoryMixField, type HistoryMix } from "@/components/history-mix-field";

const radio = (name: RegExp | string) => screen.getByRole("radio", { name });

describe("HistoryMixField", () => {
  it("selects the preset the two counts match", () => {
    render(<HistoryMixField value={{ favourites: 6, older: 6 }} onChange={() => {}} />);
    expect(radio("Balanced")).toHaveAttribute("aria-checked", "true");
    expect(radio("Recent only")).toHaveAttribute("aria-checked", "false");
    expect(screen.queryByLabelText(/long-time favourites/)).toBeNull();
  });

  it("sets both counts from a preset", async () => {
    const onChange = vi.fn();
    render(<HistoryMixField value={{ favourites: 0, older: 0 }} onChange={onChange} />);
    await userEvent.click(radio("A little older"));
    expect(onChange).toHaveBeenCalledWith({ favourites: 3, older: 3 });
  });

  it("is Custom, with both inputs, when the counts match no preset", () => {
    render(<HistoryMixField value={{ favourites: 6, older: 2 }} onChange={() => {}} />);
    expect(radio("Custom")).toHaveAttribute("aria-checked", "true");
    expect((screen.getByLabelText(/long-time favourites/) as HTMLInputElement).value).toBe("6");
    expect((screen.getByLabelText(/older watches/) as HTMLInputElement).value).toBe("2");
  });

  it("reveals the inputs on Custom without changing the counts", async () => {
    const onChange = vi.fn();
    render(<HistoryMixField value={{ favourites: 3, older: 3 }} onChange={onChange} />);
    await userEvent.click(radio("Custom"));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByLabelText(/older watches/)).toBeInTheDocument();
  });

  it("clamps a custom entry to 0-10", async () => {
    const onChange = vi.fn();
    render(<HistoryMixField value={{ favourites: 6, older: 2 }} onChange={onChange} />);
    const field = screen.getByLabelText(/long-time favourites/);
    await userEvent.clear(field);
    await userEvent.type(field, "99");
    await userEvent.tab();
    expect(onChange).toHaveBeenCalledWith({ favourites: 10, older: 2 });
  });

  it("offers the inherited option first, and picking it sends null", async () => {
    const onChange = vi.fn();
    render(
      <HistoryMixField
        value={{ favourites: 3, older: 3 }}
        onChange={onChange}
        inherited={{ prefix: "Server default", mix: { favourites: 6, older: 6 } }}
      />,
    );
    const radios = screen.getAllByRole("radio");
    expect(radios[0]).toHaveTextContent("Server default (Balanced)");
    await userEvent.click(radios[0]!);
    expect(onChange).toHaveBeenCalledWith(null);
  });

  it("selects the inherited option while the value is null, and Custom starts from the inherited counts", async () => {
    function Harness() {
      const [value, setValue] = useState<HistoryMix | null>(null);
      return (
        <HistoryMixField
          value={value}
          onChange={setValue}
          inherited={{ prefix: "Row default", mix: { favourites: 6, older: 6 } }}
        />
      );
    }
    render(<Harness />);
    expect(radio(/Row default \(Balanced\)/)).toHaveAttribute("aria-checked", "true");
    await userEvent.click(radio("Custom"));
    expect((screen.getByLabelText(/long-time favourites/) as HTMLInputElement).value).toBe("6");
    expect(radio("Custom")).toHaveAttribute("aria-checked", "true");
  });

  it("shows the mode line", () => {
    render(<HistoryMixField value={{ favourites: 0, older: 0 }} onChange={() => {}} modeLine="Each one is one more Exa search, cached a week." />);
    expect(screen.getByText(/one more Exa search/)).toBeInTheDocument();
  });
});
