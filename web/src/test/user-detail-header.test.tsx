import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen } from "@testing-library/react";
import { MemoryRouter } from "react-router";
import { describe, expect, it } from "vitest";

import { UserDetailHeader } from "@/components/user-detail/user-detail-header";
import type { User } from "@/lib/types";

const SARAH: User = {
  manage_sharing: true,
  id: 4,
  username: "sarah",
  slug: "sarah",
  user_type: "shared",
  restricted: false,
  enabled: true,
  cold_start: false,
  history_depth: 120,
  last_run_at: null,
  request_tag: "",
  requested_by_tag: "",
  picks_watched_30d: null,
  last_pick_watched_at: null,
  nickname: "",
  friendly_name: "",
  display_name: "",
  avatar_url: "",
  plex_account_id: 0,
  restriction_profile: "",
  unhidden_rows: 0,
  departed: false,
  preview_titles: [],
  prefs: {},
};

function renderHeader(user: User) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <UserDetailHeader user={user} />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

describe("UserDetailHeader picks watched", () => {
  it("says how many picks were watched in 30 days", () => {
    renderHeader({ ...SARAH, picks_watched_30d: 3 });
    expect(screen.getByText(/3 picks watched in\s+30 days/)).toBeInTheDocument();
  });

  it("uses the singular for exactly one pick", () => {
    renderHeader({ ...SARAH, picks_watched_30d: 1 });
    expect(screen.getByText(/1 pick watched in\s+30 days/)).toBeInTheDocument();
  });

  it("says 0 picks rather than dropping the figure when none were watched lately", () => {
    renderHeader({ ...SARAH, picks_watched_30d: 0 });
    expect(screen.getByText(/0 picks watched in\s+30 days/)).toBeInTheDocument();
  });

  it("leaves the figure out for someone who has never had a pick", () => {
    renderHeader(SARAH);
    expect(screen.queryByText(/picks? watched/)).toBeNull();
  });
});
