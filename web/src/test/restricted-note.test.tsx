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
    manage_sharing: true,
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
    unhidden_rows: 0,
    departed: false,
    prefs: {},
    ...patch,
  };
}

describe("RestrictedNote", () => {
  it("leads with the person and the number, not with the mechanism", () => {
    // The owner's first question is "who, and how bad" — not "what is a share filter". The headline
    // answers it; everything technical comes after.
    render(
      <RestrictedNote
        user={user({
          username: "kid",
          restriction_profile: "older_kid",
          unhidden_rows: 3,
        })}
      />,
    );

    expect(
      screen.getByText(/kid can see 3 rows that belong to other people/i),
    ).toBeInTheDocument();
  });

  it("counts one exposed row in the singular", () => {
    // "can see 1 rows" reads as a bug in the tool reporting a privacy problem, which is the worst
    // possible moment to look unreliable.
    render(
      <RestrictedNote
        user={user({ restriction_profile: "little_kid", unhidden_rows: 1 })}
      />,
    );

    expect(screen.getByText(/1 row that belongs to/i)).toBeInTheDocument();
  });

  it("says plainly that nothing in Shortlist can fix it", () => {
    // Without this the owner hunts for a setting that does not exist. Plex refuses the write; that
    // is the whole reason the alert exists, so it has to be stated, not implied.
    render(
      <RestrictedNote
        user={user({ restriction_profile: "older_kid", unhidden_rows: 2 })}
      />,
    );

    expect(
      screen.getByText(/can[’']t be fixed from here/i),
    ).toBeInTheDocument();
  });

  it("gives the one remedy that works, with the exact Plex setting", () => {
    render(
      <RestrictedNote
        user={user({ restriction_profile: "older_kid", unhidden_rows: 2 })}
      />,
    );

    expect(screen.getByText(/Restriction Profile/)).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument();
  });

  it("says outright that disabling them does NOT fix it", () => {
    // It looks like the in-app fix and it is not one: disabling removes THEIR row, while the exposure
    // is their view of everyone else's — which needs the very share filter Plex is refusing. Left
    // unsaid, the obvious next click is the one that changes nothing.
    render(
      <RestrictedNote
        user={user({ restriction_profile: "older_kid", unhidden_rows: 2 })}
      />,
    );

    // The sentence is split by a <strong>, so match the container rather than a single text node.
    // The sentence is split by a <strong>, so match on textContent — scoped to <p> so the assertion
    // is about the paragraph and not every ancestor that happens to contain it.
    expect(
      screen.getByText(
        (_, el) =>
          el?.tagName === "P" &&
          /does not fix this/i.test(el.textContent ?? ""),
      ),
    ).toBeTruthy();
  });

  it("names the profile that is set, and the setting that clears it", () => {
    // "Restricted" tells the owner nothing actionable. The profile name plus the exact Plex setting
    // is the difference between an explanation and a dead end.
    render(
      <RestrictedNote user={user({ restriction_profile: "older_kid" })} />,
    );

    expect(screen.getByText(/Older Kid/)).toBeInTheDocument();
    expect(screen.getByText(/Restriction Profile/)).toBeInTheDocument();
    expect(screen.getByText("None")).toBeInTheDocument(); // the exact value to set, not prose
  });

  it("says BOTH consequences, because they have different fixes in the reader's head", () => {
    // Plex usually hides the collections (a row would be invisible) AND refuses the privacy filters
    // (Shortlist cannot make the account private). Naming only the first leaves the second a mystery.
    render(
      <RestrictedNote user={user({ restriction_profile: "little_kid" })} />,
    );

    expect(screen.getByText(/usually hides collections/i)).toBeInTheDocument();
    expect(
      screen.getByText(/won[’']t save the privacy filters/i),
    ).toBeInTheDocument();
  });

  it("never states as fact that Plex hides collections from these accounts", () => {
    // The claim this note used to make ("Plex hides every collection from an account with a
    // restriction profile") is FALSE for `older_kid`, measured on a real server 2026-08-11: such an
    // account listed three collections. Shortlist skipped those accounts on the strength of that
    // sentence, so it was not merely wrong copy — it was the reasoning behind a privacy gap.
    render(
      <RestrictedNote user={user({ restriction_profile: "older_kid" })} />,
    );

    expect(screen.queryByText(/hides every collection/i)).toBeNull();
  });

  it("stays the calm explanation when nothing is exposed", () => {
    // `little_kid` really does see nothing, and most `older_kid` accounts see nothing either once
    // Plex's content filter empties the row. Alarming those owners would train them to ignore it.
    render(
      <RestrictedNote
        user={user({ restriction_profile: "little_kid", unhidden_rows: 0 })}
      />,
    );

    expect(screen.queryByText(/can see/i)).toBeNull();
    expect(screen.queryByRole("listitem")).toBeNull();
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
