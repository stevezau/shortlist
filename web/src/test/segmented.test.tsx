import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { Segmented } from "@/components/segmented";

const OPTIONS = [
  { value: "a", label: "Apple" },
  { value: "b", label: "Banana" },
];

describe("Segmented", () => {
  it("marks the active option pressed and the rest not", () => {
    render(<Segmented value="a" options={OPTIONS} onChange={() => {}} />);

    expect(screen.getByRole("button", { name: "Apple" })).toHaveAttribute(
      "aria-pressed",
      "true",
    );
    expect(screen.getByRole("button", { name: "Banana" })).toHaveAttribute(
      "aria-pressed",
      "false",
    );
  });

  it("calls onChange with the clicked option's value", async () => {
    const onChange = vi.fn();
    render(<Segmented value="a" options={OPTIONS} onChange={onChange} />);

    await userEvent.click(screen.getByRole("button", { name: "Banana" }));

    expect(onChange).toHaveBeenCalledExactlyOnceWith("b");
  });

  it("wraps the buttons in a labelled fieldset when a legend is given", () => {
    render(
      <Segmented
        legend="Fruit"
        value="a"
        options={OPTIONS}
        onChange={() => {}}
      />,
    );

    expect(screen.getByRole("group", { name: "Fruit" })).toBeInTheDocument();
  });

  it("exposes an aria-label group when there is no visible legend", () => {
    render(
      <Segmented
        ariaLabel="Fruit choice"
        value="a"
        options={OPTIONS}
        onChange={() => {}}
      />,
    );

    expect(
      screen.getByRole("group", { name: "Fruit choice" }),
    ).toBeInTheDocument();
  });
});

describe("Segmented — joined", () => {
  it("draws ONE control: every option in a single bordered bar, raised and amber-edged when chosen", () => {
    render(<Segmented joined ariaLabel="Show" value="b" options={OPTIONS} onChange={() => {}} />);

    const group = screen.getByRole("group", { name: "Show" });
    const bar = screen.getByRole("button", { name: "Apple" }).parentElement as HTMLElement;
    expect(group).toContainElement(bar);
    expect(bar).toContainElement(screen.getByRole("button", { name: "Banana" }));
    expect(bar.className).toMatch(/\bborder\b/);
    const chosen = screen.getByRole("button", { name: "Banana" });
    expect(chosen.classList.contains("bg-raised")).toBe(true);
    expect(chosen.classList.contains("shadow-selected-x")).toBe(true);
    expect(chosen.classList.contains("bg-primary")).toBe(false);
    expect(screen.getByRole("button", { name: "Apple" }).classList.contains("bg-raised")).toBe(false);
  });

  it("still reports which option is pressed and changes on click", async () => {
    const onChange = vi.fn();
    render(<Segmented joined ariaLabel="Show" value="a" options={OPTIONS} onChange={onChange} />);

    expect(screen.getByRole("button", { name: "Apple" })).toHaveAttribute("aria-pressed", "true");
    await userEvent.click(screen.getByRole("button", { name: "Banana" }));
    expect(onChange).toHaveBeenCalledExactlyOnceWith("b");
  });
});
