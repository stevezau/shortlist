import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import {
  ratingVerdict,
  WatchRating,
} from "@/components/user-detail/watch-rating";
import type { WatchedPage } from "@/lib/types";

/** Ratings on, this account believed — the state a real viewer is in. */
const ON: Pick<WatchedPage, "dislike_threshold" | "ratings_trusted"> = {
  dislike_threshold: 2,
  ratings_trusted: true,
};

describe("ratingVerdict", () => {
  it("says nothing at all about a title nobody rated", () => {
    // The ~99.7% case. `null` must not read as a zero, which would be the lowest possible rating.
    expect(ratingVerdict(null, ON)).toBeNull();
    expect(ratingVerdict(undefined, ON)).toBeNull();
  });

  it("converts Plex's 0..10 scale to the five stars people actually see", () => {
    expect(ratingVerdict(10, ON)?.stars).toBe(5);
    expect(ratingVerdict(7, ON)?.stars).toBe(3.5);
    expect(ratingVerdict(2, ON)?.stars).toBe(1);
  });

  it("blocks at or below the threshold, inclusive", () => {
    expect(ratingVerdict(2, ON)?.blocked).toBe(true);
    expect(ratingVerdict(0, ON)?.blocked).toBe(true);
    expect(ratingVerdict(4, ON)?.blocked).toBe(false);
  });

  it("shows a tool-written rating but never acts on it", () => {
    // Mirrors `is_human_rating` on the backend. If these two ever disagree the page claims a rating
    // is shaping picks that the run ignored, which is worse than not showing it at all.
    const verdict = ratingVerdict(1.6, ON);

    expect(verdict?.blocked).toBe(false);
    expect(verdict?.ignoredReason).toBe("tool-written");
  });

  it("acts on nothing when the whole account is distrusted", () => {
    const verdict = ratingVerdict(2, {
      dislike_threshold: 2,
      ratings_trusted: false,
    });

    expect(verdict?.blocked).toBe(false);
    expect(verdict?.ignoredReason).toBe("distrusted-account");
  });

  it("acts on nothing when the feature is switched off", () => {
    const verdict = ratingVerdict(2, {
      dislike_threshold: null,
      ratings_trusted: true,
    });

    expect(verdict?.blocked).toBe(false);
    expect(verdict?.ignoredReason).toBe("switched-off");
  });
});

describe("WatchRating", () => {
  it("renders nothing for an unrated title rather than an empty placeholder", () => {
    const { container } = render(<WatchRating rating={null} page={ON} />);

    expect(container).toBeEmptyDOMElement();
  });

  it("says a low-rated title is not seeding, and why", () => {
    render(<WatchRating rating={2} page={ON} />);

    expect(screen.getByText(/not seeding/)).toBeInTheDocument();
    expect(
      screen.getByTitle(/isn’t used to find similar titles for them/),
    ).toBeInTheDocument();
  });

  it("shows a high rating without claiming it changed anything", () => {
    render(<WatchRating rating={10} page={ON} />);

    expect(screen.queryByText(/not seeding/)).not.toBeInTheDocument();
    expect(
      screen.getByTitle("They rated this 5 out of 5 in Plex"),
    ).toBeInTheDocument();
  });

  it("explains a tool-written rating instead of silently showing it", () => {
    render(<WatchRating rating={7.9} page={ON} />);

    expect(
      screen.getByTitle(/Plex only writes whole numbers/),
    ).toBeInTheDocument();
  });
});
