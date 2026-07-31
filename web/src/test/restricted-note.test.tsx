import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { RestrictedNote } from "@/components/restricted-note";
import type { User } from "@/lib/types";

/**
 * The note that explains why an account gets no row. It used to render for every Plex Home user,
 * because plex.tv reports `restricted` for all of them — telling owners of an ordinary managed account
 * that Plex was hiding content from them, which was untrue (#20).
 */
function user(patch: Partial<User> = {}): User {
  return {
    id: 9,
    username: "kid",
    slug: "kid",
    user_type: "managed",
    restricted: true,
    enabled: false,
    cold_start: false,
    history_depth: 0,
    last_run_at: null,
    request_tag: "",
    hit_rate: null,
    nickname: "",
    friendly_name: "",
    display_name: "",
    avatar_url: "",
    plex_account_id: 0,
    restriction_profile: "",
    preview_titles: [],
    prefs: {},
    ...patch,
  };
}

describe("RestrictedNote", () => {
  it("names the profile that is set, and the setting that clears it", async () => {
    // "Restricted" tells the owner nothing actionable. The profile name plus the exact Plex setting
    // is the difference between an explanation and a dead end.
    render(<RestrictedNote user={user({ restriction_profile: "older_kid" })} />);

    expect(screen.getByText(/Older Kid/)).toBeInTheDocument();
    expect(screen.getByText(/Restriction Profile/)).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();  // the exact value to set, not prose
  });

  it("says BOTH consequences, because they have different fixes in the reader's head", () => {
    // Plex hides the collections (a row would be invisible) AND refuses the share filters (Shortlist
    // cannot make the account private). Naming only the first leaves the second a mystery.
    render(<RestrictedNote user={user({ restriction_profile: "little_kid" })} />);

    expect(screen.getByText(/hides every collection/i)).toBeInTheDocument();
    expect(screen.getByText(/refuses to save the privacy filters/i)).toBeInTheDocument();
  });

  it("renders nothing for a managed account with no profile", () => {
    // The #20 case: Plex hides nothing from them, and they now get a row and filters like anyone.
    const { container } = render(
      <RestrictedNote user={user({ restriction_profile: "" })} />,
    );

    expect(container).toBeEmptyDOMElement();
  });

  it("renders nothing when the field is absent entirely", () => {
    // A user row served before the roster sync backfills the column has no `restriction_profile` at
    // all. `undefined` must read as "no profile", not as a crash or a spurious warning.
    const { container } = render(<RestrictedNote user={user()} />);

    expect(container).toBeEmptyDOMElement();
  });
});
