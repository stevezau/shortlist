import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { RowSectionNavigation, type RowSection } from "@/components/rows/row-section-navigation";

const SECTIONS: RowSection[] = [
  { id: "name-and-look", label: "Name & look" },
  { id: "who-gets-it", label: "Who gets it" },
  { id: "what-goes-in", label: "What goes in" },
  { id: "schedule", label: "Schedule" },
  { id: "placement", label: "Placement" },
  { id: "requests", label: "Requests" },
  { id: "danger-zone", label: "Danger zone" },
];

/** Captures the observer the jump list creates, so a test can say which sections are in view. */
let observed: { callback: IntersectionObserverCallback; targets: Element[] } | null = null;

class FakeIntersectionObserver {
  constructor(callback: IntersectionObserverCallback) {
    observed = { callback, targets: [] };
  }
  observe(target: Element) {
    observed?.targets.push(target);
  }
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
}

function inView(...ids: string[]) {
  const entries = (observed?.targets ?? []).map(
    (target) => ({ target, isIntersecting: ids.includes(target.id) }) as IntersectionObserverEntry,
  );
  act(() => observed?.callback(entries, {} as IntersectionObserver));
}

function Example() {
  return (
    <div>
      <RowSectionNavigation sections={SECTIONS} />
      {SECTIONS.map((section) => (
        <section key={section.id} id={section.id} aria-labelledby={`${section.id}-heading`}>
          <h2 id={`${section.id}-heading`} tabIndex={-1}>
            {section.label}
          </h2>
          <input aria-label={`${section.label} draft`} defaultValue="Unsaved value" />
        </section>
      ))}
    </div>
  );
}

beforeEach(() => {
  observed = null;
  vi.stubGlobal("IntersectionObserver", FakeIntersectionObserver);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

describe("Row section jump list", () => {
  it("lists every section in order as a link to it, with every draft field left on the page", () => {
    render(<Example />);
    const nav = screen.getByRole("navigation", { name: "Row settings sections" });
    const links = Array.from(nav.querySelectorAll("a"));

    expect(links.map((link) => link.textContent)).toEqual(SECTIONS.map((section) => section.label));
    expect(links.map((link) => link.getAttribute("href"))).toEqual(SECTIONS.map((section) => `#${section.id}`));
    for (const section of SECTIONS) {
      expect(screen.getByLabelText(`${section.label} draft`)).toHaveValue("Unsaved value");
    }
  });

  it("marks the first section current before anything has scrolled", () => {
    render(<Example />);
    expect(screen.getByRole("link", { name: "Name & look" })).toHaveAttribute("aria-current", "location");
  });

  it("follows the section in view, the topmost when several are", () => {
    render(<Example />);
    inView("schedule", "placement");

    expect(screen.getByRole("link", { name: "Schedule" })).toHaveAttribute("aria-current", "location");
    expect(screen.getByRole("link", { name: "Name & look" })).not.toHaveAttribute("aria-current");

    inView("placement");
    expect(screen.getByRole("link", { name: "Placement" })).toHaveAttribute("aria-current", "location");
  });

  it("keeps the last section current while the gap between two sections is in view", () => {
    render(<Example />);
    inView("requests");
    inView();
    expect(screen.getByRole("link", { name: "Requests" })).toHaveAttribute("aria-current", "location");
  });

  it("jumps from the keyboard: scrolls the section in and moves focus to its heading", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    render(<Example />);

    const link = screen.getByRole("link", { name: "Who gets it" });
    link.focus();
    await userEvent.keyboard("{Enter}");

    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "smooth", block: "start" });
    expect(screen.getByRole("heading", { name: "Who gets it" })).toHaveFocus();
    expect(link).toHaveAttribute("aria-current", "location");
  });

  it("jumps without animation when reduced motion is asked for", async () => {
    const scrollIntoView = vi.fn();
    Element.prototype.scrollIntoView = scrollIntoView;
    vi.stubGlobal("matchMedia", vi.fn().mockReturnValue({ matches: true }));
    render(<Example />);

    await userEvent.click(screen.getByRole("link", { name: "Danger zone" }));

    expect(scrollIntoView).toHaveBeenCalledWith({ behavior: "auto", block: "start" });
  });

  it("observes every section it lists", () => {
    render(<Example />);
    expect(observed?.targets.map((target) => target.id)).toEqual(SECTIONS.map((section) => section.id));
  });
});
