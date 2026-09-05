import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PickList } from "@/components/pick-list";
import { TitlePoster } from "@/components/title-poster";
import type { Pick } from "@/lib/types";

function pick(overrides: Partial<Pick> = {}): Pick {
  return {
    rank: 1,
    title: "Arrival",
    reason: "slow, thoughtful sci-fi",
    rating_key: 575662,
    seed_title: null,
    sources: [],
    affinity: 1,
    ...overrides,
  } as Pick;
}

/** The rendered box, whether it came back as an `<img>` or as the placeholder `<div>`. */
function boxOf(container: HTMLElement): HTMLElement {
  const el = container.querySelector("img, div[aria-hidden='true']");
  if (!el) throw new Error("no poster box rendered");
  return el as HTMLElement;
}

describe("TitlePoster", () => {
  it("a pick uses the local PMS proxy and an inbox title uses the TMDB CDN", () => {
    // One component, two sources — so the pick list and the request inbox cannot drift apart.
    const { container: pickBox } = render(<TitlePoster ratingKey={575662} />);
    expect(pickBox.querySelector("img")).toHaveAttribute(
      "src",
      "/api/picks/575662/poster",
    );

    const { container: inboxBox } = render(
      <TitlePoster posterPath="/abc.jpg" />,
    );
    expect(inboxBox.querySelector("img")).toHaveAttribute(
      "src",
      "https://image.tmdb.org/t/p/w154/abc.jpg",
    );
  });

  it("keeps the owner's Plex token out of the browser entirely", () => {
    // The proxy exists so the token stays server-side (plex-safety rule 9). An `<img src>` is the
    // single most quotable URL in a browser — history, referrers, extensions.
    const { container } = render(<TitlePoster ratingKey={575662} />);

    expect(container.querySelector("img")!.getAttribute("src")).not.toMatch(
      /token/i,
    );
  });

  it("renders the placeholder with no network request when there is no rating key and no poster path", () => {
    const { container } = render(<TitlePoster />);

    expect(container.querySelector("img")).toBeNull();
    expect(boxOf(container)).toBeInTheDocument();
  });

  it("treats a rating key of 0 as no artwork, so an unmatched pick never asks Plex", () => {
    // `picker.py` stores `c.rating_key or 0`; asking the server about it costs a round-trip to be
    // told 404.
    const { container } = render(<TitlePoster ratingKey={0} />);

    expect(container.querySelector("img")).toBeNull();
  });

  it("falls back to a placeholder of the same size when the image errors", () => {
    const { container } = render(<TitlePoster ratingKey={575662} />);
    const before = boxOf(container).className;

    fireEvent.error(container.querySelector("img")!);

    const after = boxOf(container);
    expect(container.querySelector("img")).toBeNull();
    // Same box, so a title with no art does not reflow the row around it.
    for (const size of ["h-[60px]", "w-[40px]", "sm:h-[87px]", "sm:w-[58px]"]) {
      expect(before).toContain(size);
      expect(after.className).toContain(size);
    }
  });

  it("is decorative, because the title is already beside it as real text", () => {
    const { container } = render(<TitlePoster ratingKey={1} />);

    expect(container.querySelector("img")).toHaveAttribute("alt", "");
    expect(container.querySelector("img")).toHaveAttribute("loading", "lazy");
  });
});

describe("PickList posters", () => {
  it("every pick reserves its poster box, so a title with no art does not reflow the list", () => {
    const { container } = render(
      <PickList
        picks={[
          pick({ rank: 1, title: "Arrival", rating_key: 575662 }),
          pick({ rank: 2, title: "Never matched", rating_key: 0 }),
        ]}
      />,
    );

    const boxes = Array.from(
      container.querySelectorAll("img, div[aria-hidden='true']"),
    );
    expect(boxes).toHaveLength(2);
    expect(boxes[1]?.tagName).toBe("DIV"); // the placeholder, at the same size
  });

  it("collapsed picks are absent from the DOM, so their posters are never requested", () => {
    const { container } = render(
      <PickList
        picks={[
          pick({ rank: 1, rating_key: 1 }),
          pick({ rank: 2, rating_key: 2 }),
          pick({ rank: 3, rating_key: 3 }),
        ]}
        collapseAfter={1}
      />,
    );

    expect(container.querySelectorAll("img")).toHaveLength(1);
    expect(screen.getByRole("button", { name: /show all 3/i })).toBeVisible();
  });

  it("requests the rest only once the owner expands the list", () => {
    const { container } = render(
      <PickList
        picks={[
          pick({ rank: 1, rating_key: 1 }),
          pick({ rank: 2, rating_key: 2 }),
        ]}
        collapseAfter={1}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: /show all 2/i }));

    expect(container.querySelectorAll("img")).toHaveLength(2);
  });

  it("still renders the title and reason beside the poster", () => {
    render(
      <PickList picks={[pick({ title: "Arrival", reason: "slow sci-fi" })]} />,
    );

    expect(screen.getByText("Arrival")).toBeVisible();
    expect(screen.getByText(/slow sci-fi/)).toBeVisible();
  });
});
