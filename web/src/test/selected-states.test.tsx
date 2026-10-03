/**
 * One look for "this one is chosen", and it is never filled amber.
 *
 * Amber used to mark the primary action AND the active nav item, the selected tab, the picked
 * segment, preset chips and rank numbers, so it meant both "selected" and "do this". The design
 * refresh keeps filled amber for the one primary button per screen; every selected state is a
 * raised neutral surface with a 2px amber edge (`lib/selected.ts`).
 */

import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { NumberPresets } from "@/components/number-presets";
import { Segmented } from "@/components/segmented";
import { Badge } from "@/components/ui/badge";
import { Switch } from "@/components/ui/switch";
import { Tabs } from "@/components/ui/tabs";

describe("selected states", () => {
  it("raises the selected segment instead of filling it amber", () => {
    render(
      <Segmented
        value="b"
        ariaLabel="Fruit"
        options={[
          { value: "a", label: "Apple" },
          { value: "b", label: "Banana" },
        ]}
        onChange={() => {}}
      />,
    );

    const selected = screen.getByRole("button", { name: "Banana" });
    expect(selected).toHaveAttribute("aria-pressed", "true");
    expect(selected.classList.contains("bg-primary")).toBe(false);
    expect(selected.classList.contains("bg-raised")).toBe(true);
    expect(screen.getByRole("button", { name: "Apple" }).classList.contains("bg-raised")).toBe(false);
  });

  it("raises the selected preset chip, and the Custom chip when it is open", () => {
    const { rerender } = render(
      <NumberPresets
        value={30}
        presets={[
          { value: 15, label: "15s" },
          { value: 30, label: "30s" },
        ]}
        onChange={() => {}}
        min={1}
        max={600}
        ariaLabel="Plex timeout"
      />,
    );

    const preset = screen.getByRole("button", { name: "30s" });
    expect(preset).toHaveAttribute("aria-pressed", "true");
    expect(preset.classList.contains("bg-primary")).toBe(false);
    expect(preset.classList.contains("bg-raised")).toBe(true);

    rerender(
      <NumberPresets
        value={42}
        presets={[
          { value: 15, label: "15s" },
          { value: 30, label: "30s" },
        ]}
        onChange={() => {}}
        min={1}
        max={600}
        ariaLabel="Plex timeout"
      />,
    );
    const custom = screen.getByRole("button", { name: "Custom…" });
    expect(custom).toHaveAttribute("aria-pressed", "true");
    expect(custom.classList.contains("bg-primary")).toBe(false);
  });

  it("marks the selected tab with foreground text and an amber edge, not amber text", () => {
    render(
      <Tabs
        id="t"
        ariaLabel="Sections"
        value="log"
        onChange={() => {}}
        options={[
          { value: "rows", label: "Rows" },
          { value: "log", label: "Log" },
        ]}
      />,
    );

    const selected = screen.getByRole("tab", { name: "Log" });
    expect(selected).toHaveAttribute("aria-selected", "true");
    expect(selected.classList.contains("text-primary")).toBe(false);
    expect(selected.classList.contains("text-foreground")).toBe(true);
    expect(selected.classList.contains("border-primary")).toBe(true);
  });

  it("keeps the default badge neutral, with amber only on the explicit accent variant", () => {
    render(
      <>
        <Badge>Plain</Badge>
        <Badge variant="accent">Loud</Badge>
      </>,
    );

    expect(screen.getByText("Plain").classList.contains("bg-primary")).toBe(false);
    expect(screen.getByText("Loud").className).toMatch(/primary/);
  });

  it("draws an on-switch as a light neutral track, never amber", () => {
    render(
      <>
        <Switch aria-label="On" checked onCheckedChange={() => {}} />
        <Switch aria-label="Off" checked={false} onCheckedChange={() => {}} />
      </>,
    );

    const on = screen.getByRole("switch", { name: "On" });
    const off = screen.getByRole("switch", { name: "Off" });
    expect(on).toHaveAttribute("data-state", "checked");
    expect(off).toHaveAttribute("data-state", "unchecked");
    // The track colour is a data-state variant, so assert the variant the component ships.
    expect(on.className).toContain("data-[state=checked]:bg-foreground");
    expect(on.className).not.toMatch(/bg-primary/);
    expect(off.className).toContain("data-[state=unchecked]:bg-input");
  });
});
