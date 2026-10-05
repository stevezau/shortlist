import { render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";

import { RowPlexCard } from "@/components/rows/row-plex-card";
import { blankInput } from "@/lib/collections";
import type { CollectionInput } from "@/lib/types";

import { HALLOWEEN } from "./season-fixtures";

function input(patch: Partial<CollectionInput> = {}): CollectionInput {
  return { ...blankInput(), name: "Hidden Gems", ...patch };
}

describe("RowPlexCard", () => {
  it("fills the name and description in for the sample person, keeping the description's line breaks", () => {
    render(
      <RowPlexCard
        input={input({
          name_template: "{user}'s {library_name} picks",
          description: "Picked for {user}.\nFrom {library_name}.",
        })}
        collectionId={null}
        hasImage={false}
      />,
    );

    expect(screen.getByText("“Sarah's Movies picks”")).toBeInTheDocument();
    // A {library_name} title collapses its whitespace; a description must not.
    const description = screen.getByText(/Picked for Sarah\./);
    expect(description.textContent).toBe("Picked for Sarah.\nFrom Movies.");
    expect(screen.getByText(/each person gets their own name/i)).toBeInTheDocument();
  });

  it("fills the season into the description as it does the name", () => {
    render(
      <RowPlexCard
        input={input({ name_template: "{season} picks", description: "{season_emoji} {season} favourites", seasons: ["halloween"] })}
        collectionId={null}
        hasImage={false}
        sampleSeason={HALLOWEEN}
      />,
    );

    expect(screen.getByText("🎃 Halloween favourites")).toBeInTheDocument();
  });

  it("says a seasonal name follows the season rather than the person", () => {
    render(<RowPlexCard input={input({ name_template: "{season} for {user}" })} collectionId={null} hasImage={false} />);

    expect(screen.getByText(/the name follows the season/i)).toBeInTheDocument();
    expect(screen.queryByText(/each person gets their own name/i)).not.toBeInTheDocument();
  });

  it("says Plex keeps its own artwork when the row sets no poster", () => {
    render(<RowPlexCard input={input()} collectionId={1} hasImage />);

    expect(screen.getByText("Plex’s own artwork")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });

  it("shows the uploaded image only once there is one", () => {
    const upload = input({ poster: { mode: "upload", title: "", subtitle: "", style: "" } });
    const { unmount } = render(<RowPlexCard input={upload} collectionId={7} hasImage={false} />);
    expect(screen.getByText("No image uploaded yet")).toBeInTheDocument();
    unmount();

    render(<RowPlexCard input={upload} collectionId={7} hasImage />);
    expect(screen.getByRole("img", { name: "This row's poster" })).toHaveAttribute(
      "src",
      expect.stringContaining("/collections/7/poster/image"),
    );
  });

  it("shows a text poster's own words, filled in, rather than inventing a picture", () => {
    render(
      <RowPlexCard
        input={input({ poster: { mode: "text", title: "{user}'s Picks", subtitle: "From {library_name}", style: "" } })}
        collectionId={null}
        hasImage={false}
      />,
    );

    expect(screen.getByText("Sarah's Picks")).toBeInTheDocument();
    expect(screen.getByText("From Movies")).toBeInTheDocument();
    expect(screen.queryByRole("img")).not.toBeInTheDocument();
  });
});


it("shrinks poster lettering when long words would overflow the thumbnail", () => {
  const width = vi.spyOn(HTMLElement.prototype, "clientWidth", "get").mockReturnValue(112);
  const height = vi.spyOn(HTMLElement.prototype, "clientHeight", "get").mockReturnValue(168);
  const contentHeight = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockImplementation(function (this: HTMLElement) {
    // A narrow poster with a long title needs twelve lines in this measured layout.
    return this.style.fontSize ? parseFloat(this.style.fontSize) * 12 : 0;
  });
  const contentWidth = vi.spyOn(HTMLElement.prototype, "scrollWidth", "get").mockReturnValue(92);
  try {
    render(<RowPlexCard compact input={input({ poster: { mode: "text", title: "A very long title to fit on a small poster", subtitle: "Selected for you", style: "" } })} collectionId={null} hasImage={false} />);
    const words = screen.getByText("A very long title to fit on a small poster").parentElement!;
    expect(parseFloat(words.style.fontSize)).toBeLessThan(16);
    expect(words.scrollHeight).toBeLessThanOrEqual(148);
    expect(screen.getByText("Selected for you")).toBeInTheDocument();
  } finally {
    width.mockRestore(); height.mockRestore(); contentHeight.mockRestore(); contentWidth.mockRestore();
  }
});
