import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { GatedSwitch } from "@/components/ui/gated-switch";

const REASON = "Clear the Restriction Profile in Plex to enable.";

describe("GatedSwitch", () => {
  it("is an ordinary switch when there is no reason to gate it", async () => {
    const onCheckedChange = vi.fn();
    render(
      <GatedSwitch
        checked={false}
        onCheckedChange={onCheckedChange}
        aria-label="Row"
      />,
    );

    const toggle = screen.getByRole("switch", { name: "Row" });
    expect(toggle).not.toHaveAttribute("aria-disabled");
    expect(toggle).not.toHaveAttribute("aria-describedby");

    await userEvent.click(toggle);
    expect(onCheckedChange).toHaveBeenCalledWith(true);
  });

  it("uses aria-disabled, never the native disabled attribute", () => {
    // The whole point of the component. `disabled` takes the control out of the tab order, and the
    // explanation goes with it — a keyboard user meets a dead switch and no reason at all.
    render(
      <GatedSwitch
        checked={false}
        onCheckedChange={vi.fn()}
        reason={REASON}
        aria-label="Row"
      />,
    );

    const toggle = screen.getByRole("switch", { name: "Row" });
    expect(toggle).not.toBeDisabled();
    expect(toggle).toHaveAttribute("aria-disabled", "true");
  });

  it("puts the reason where a screen reader will read it, not only in a hover title", () => {
    render(
      <GatedSwitch
        checked={false}
        onCheckedChange={vi.fn()}
        reason={REASON}
        aria-label="Row"
      />,
    );

    const toggle = screen.getByRole("switch", { name: "Row" });
    const reasonId = toggle.getAttribute("aria-describedby") ?? "";
    expect(reasonId).not.toBe("");
    expect(document.getElementById(reasonId)?.textContent).toBe(REASON);
    expect(toggle).toHaveAttribute("title", REASON);
  });

  it("swallows a click while it is gated", async () => {
    // `aria-disabled` is advisory only: Radix happily fires the change event, so without the no-op
    // guard the control would look inert and act live.
    const onCheckedChange = vi.fn();
    render(
      <GatedSwitch
        checked={false}
        onCheckedChange={onCheckedChange}
        reason={REASON}
        aria-label="Row"
      />,
    );

    await userEvent.click(screen.getByRole("switch", { name: "Row" }));

    expect(onCheckedChange).not.toHaveBeenCalled();
  });

  it("gives each gated switch its own reason element, so a list of them cannot cross-wire", () => {
    // Two switches on one page (the Users table renders one per account) must not share an id: the
    // second would silently describe itself with the first one's reason.
    render(
      <>
        <GatedSwitch
          checked={false}
          onCheckedChange={vi.fn()}
          reason="First reason."
          aria-label="First"
        />
        <GatedSwitch
          checked={false}
          onCheckedChange={vi.fn()}
          reason="Second reason."
          aria-label="Second"
        />
      </>,
    );

    const first = screen.getByRole("switch", { name: "First" });
    const second = screen.getByRole("switch", { name: "Second" });
    expect(first.getAttribute("aria-describedby")).not.toBe(
      second.getAttribute("aria-describedby"),
    );
    expect(
      document.getElementById(second.getAttribute("aria-describedby") ?? "")
        ?.textContent,
    ).toBe("Second reason.");
  });
});
