import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RowName } from "@/components/rows/row-name";

describe("RowName", () => {
  it("draws a season token as a chip when it does not know the season", () => {
    render(<RowName name="{season_emoji} {season} Favourites" />);
    expect(screen.getByText("season emoji")).toBeInTheDocument();
    expect(screen.getByText("season")).toBeInTheDocument();
  });

  it("reads the season's own words when it does", () => {
    const { container } = render(
      <RowName name="{season_emoji} {season} Favourites" season={{ name: "Halloween", emoji: "🎃" }} />,
    );
    expect(container).toHaveTextContent("🎃 Halloween Favourites");
    expect(screen.queryByText("season")).toBeNull();
  });
});
