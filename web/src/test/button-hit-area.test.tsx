import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { Button } from "@/components/ui/button";

const COARSE = "[@media(pointer:coarse)]";

describe("Button hit area on touch screens", () => {
  it.each(["sm", "icon"] as const)("gives a %s button a 44px tap target on a coarse pointer only", (size) => {
    render(<Button size={size} aria-label="Act">x</Button>);
    const classes = screen.getByRole("button", { name: "Act" }).className.split(" ");

    // The tap target is a transparent box centred on the button, so nothing changes how it looks.
    expect(classes).toContain(`${COARSE}:relative`);
    expect(classes).toContain(`${COARSE}:before:h-11`);
    expect(classes).toContain(`${COARSE}:before:min-w-11`);
    // Every hit-area class is behind the coarse-pointer query: a mouse sees the button it always saw.
    expect(classes.filter((c) => c.startsWith("before:"))).toEqual([]);
    expect(classes).not.toContain("relative");
  });

  it("leaves the default size alone", () => {
    render(<Button aria-label="Act">x</Button>);
    expect(screen.getByRole("button", { name: "Act" }).className).not.toContain(COARSE);
  });
});
