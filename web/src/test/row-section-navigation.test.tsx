import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useRef } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { RowSectionNavigation } from "@/components/rows/row-section-navigation";
import { SettingsGroup } from "@/components/rows/settings-group";

const names = ["Appearance", "Row settings", "Audience", "Titles & filters", "Schedule", "Plex placement", "Requests"];
function Example() {
  const root = useRef<HTMLDivElement>(null);
  return <div ref={root}><RowSectionNavigation root={root} showRequests />{names.map((name) => <SettingsGroup key={name} title={name} description="Settings" defaultOpen={name === "Appearance"}><input aria-label={`${name} draft`} defaultValue="Unsaved value" /></SettingsGroup>)}</div>;
}

afterEach(() => { vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

function layout() {
  let audienceTop = 700;
  vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockImplementation(function (this: HTMLElement) {
    const title = this.dataset.settingsGroup;
    const top = this.tagName === "NAV" ? 64 : title === "Appearance" ? -300 : title === "Row settings" ? -100 : title === "Audience" ? audienceTop : 2000;
    return { top, bottom: top + 300, left: 0, right: 500, width: 500, height: this.tagName === "NAV" ? 100 : 300, x: 0, y: top, toJSON() {} };
  });
  const getStyle = window.getComputedStyle.bind(window);
  vi.spyOn(window, "getComputedStyle").mockImplementation((element) => {
    const style = getStyle(element);
    if (element.tagName === "NAV") Object.defineProperty(style, "top", { value: "64px", configurable: true });
    return style;
  });
  const scroll = vi.spyOn(window, "scrollTo").mockImplementation(() => {});
  return { scroll, setAudienceTop: (value: number) => { audienceTop = value; } };
}

describe("Row section navigation", () => {
  it("uses the same names as the section headings and leaves all draft fields mounted", () => {
    render(<Example />);
    for (const name of names) {
      expect(screen.getByRole("button", { name })).toBeVisible();
      expect(screen.getByRole("heading", { name, hidden: true })).toBeInTheDocument();
      expect(screen.getByLabelText(`${name} draft`)).toHaveValue("Unsaved value");
    }
  });

  it("opens, focuses without a second scroll, offsets the heading and highlights repeated clicks", async () => {
    const { scroll } = layout();
    const focus = vi.spyOn(HTMLElement.prototype, "focus");
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Audience" }));
    const group = document.querySelector('[data-settings-group="Audience"]')!;
    expect(group).toHaveAttribute("open");
    expect(group).toHaveAttribute("data-navigation-highlight", "true");
    expect(group.querySelector("summary")).toHaveFocus();
    expect(focus).toHaveBeenCalledWith({ preventScroll: true });
    expect(scroll).toHaveBeenLastCalledWith({ top: 524, behavior: "smooth" });
    await userEvent.click(screen.getByRole("button", { name: "Audience" }));
    expect(scroll).toHaveBeenCalledTimes(2);
  });

  it("tracks manual scrolling instead of retaining the last clicked section", () => {
    const { setAudienceTop } = layout();
    render(<Example />);
    expect(screen.getByRole("button", { name: "Row settings" })).toHaveAttribute("aria-current", "location");
    setAudienceTop(150);
    fireEvent.scroll(window);
    expect(screen.getByRole("button", { name: "Audience" })).toHaveAttribute("aria-current", "location");
    expect(screen.getByRole("button", { name: "Row settings" })).not.toHaveAttribute("aria-current");
  });

  it("clears destination highlights and pending work when unmounted", () => {
    vi.useFakeTimers();
    layout();
    const remove = vi.spyOn(window, "removeEventListener");
    const { unmount } = render(<Example />);
    fireEvent.click(screen.getByRole("button", { name: "Audience" }));
    const group = document.querySelector('[data-settings-group="Audience"]')!;
    expect(group).toHaveAttribute("data-navigation-highlight", "true");
    vi.advanceTimersByTime(1400);
    expect(group).not.toHaveAttribute("data-navigation-highlight");
    fireEvent.click(screen.getByRole("button", { name: "Audience" }));
    unmount();
    expect(group).not.toHaveAttribute("data-navigation-highlight");
    expect(remove).toHaveBeenCalledWith("scroll", expect.any(Function));
    expect(remove).toHaveBeenCalledWith("resize", expect.any(Function));
    expect(vi.getTimerCount()).toBe(0);
  });

  it("uses immediate scrolling when reduced motion is requested", async () => {
    const { scroll } = layout();
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    render(<Example />);
    await userEvent.click(screen.getByRole("button", { name: "Audience" }));
    expect(scroll).toHaveBeenLastCalledWith({ top: 524, behavior: "instant" });
  });
});
