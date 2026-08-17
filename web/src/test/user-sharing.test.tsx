import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { UserSharing } from "@/components/user-detail/user-sharing";
import type { User } from "@/lib/types";

const patchUser = vi.hoisted(() => vi.fn());

vi.mock("@/lib/queries", () => ({
  usePatchUser: () => ({
    mutate: patchUser,
    isPending: false,
    isError: false,
    error: null,
  }),
}));

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
    display_name: "Ellie",
    avatar_url: "",
    plex_account_id: 500,
    restriction_profile: "",
    preview_titles: [],
    unhidden_rows: 0,
    departed: false,
    prefs: {},
    ...patch,
  };
}

function renderCard(value: User) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <UserSharing user={value} />
    </QueryClientProvider>,
  );
}

describe("UserSharing", () => {
  it("sends manage_sharing=false when the owner turns it off", async () => {
    patchUser.mockClear();
    renderCard(user());

    await userEvent.click(screen.getByRole("switch"));

    expect(patchUser).toHaveBeenCalledWith(
      { id: 9, patch: { manage_sharing: false } },
      expect.anything(),
    );
  });

  it("spells out the consequence while it is off, not just when it is set", () => {
    // The cost of this setting is that the account can see other people's rows. Hidden state that
    // changes who sees what is exactly what gets forgotten and later reported as a leak, so the
    // warning is permanent, not a one-off confirmation.
    renderCard(user({ manage_sharing: false }));

    expect(
      screen.getByText(/can see other people’s rows/i),
    ).toBeInTheDocument();
  });

  it("says nothing to warn about while Shortlist is managing them", () => {
    renderCard(user());

    expect(screen.queryByText(/can see other people’s rows/i)).toBeNull();
  });

  it("offers the owner nothing to switch, because Plex gives them no share to filter", () => {
    // Rule 5: the server owner has no share filters at all. A switch here would promise a change
    // Plex cannot make.
    renderCard(user({ user_type: "owner" }));

    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.getByText(/no sharing settings/i)).toBeInTheDocument();
  });
});
