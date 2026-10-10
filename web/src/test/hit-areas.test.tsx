import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { Switch } from "@/components/ui/switch";
import { Why } from "@/components/why";

// jsdom does no layout, so these pin the recipe: an invisible pseudo-element that reaches 24px.
describe("small controls keep a 24px hit area", () => {
  it("gives the switch a taller invisible hit box without resizing it", () => {
    render(<Switch aria-label="On" />);
    const classes = screen.getByRole("switch").className;
    expect(classes).toContain("relative");
    expect(classes).toContain("before:-inset-y-[2px]");
    expect(classes).toContain("h-5");
  });

  it("gives the why button a 24px hit box around its 14px icon", () => {
    render(<Why text="because" />);
    const classes = screen.getByRole("button", { name: "Why?" }).className;
    expect(classes).toContain("before:size-6");
  });
});
