import { render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { Tabs } from "@/components/ui/tabs";

const options = [
  { value: "a", label: "Alpha" },
  { value: "b", label: "Bravo" },
  { value: "c", label: "Charlie" },
];

function stubGeometry(clientWidth: number, scrollWidth: number) {
  vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(clientWidth);
  vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(scrollWidth);
  vi.spyOn(HTMLElement.prototype, "offsetWidth", "get").mockReturnValue(100);
  vi.spyOn(HTMLElement.prototype, "offsetLeft", "get").mockReturnValue(300);
}

describe("tab strip overflow", () => {
  afterEach(() => vi.restoreAllMocks());

  it("scrolls the active tab into view and fades the side that still has tabs", () => {
    stubGeometry(320, 700);
    render(<Tabs id="t" ariaLabel="T" value="c" onChange={() => {}} options={options} />);
    const strip = screen.getByRole("tablist");
    // centred: 300 - (320 - 100) / 2
    expect(strip.scrollLeft).toBe(190);
    expect(strip.style.getPropertyValue("--fade-right")).toBe("24px");
    expect(strip.style.getPropertyValue("--fade-left")).toBe("24px");
    expect(strip.className).toContain("[&::-webkit-scrollbar]:hidden");
  });

  it("draws no fade when every tab fits", () => {
    stubGeometry(500, 500);
    render(<Tabs id="t" ariaLabel="T" value="a" onChange={() => {}} options={options} />);
    expect(screen.getByRole("tablist").style.getPropertyValue("--fade-right")).toBe("0px");
  });
});
