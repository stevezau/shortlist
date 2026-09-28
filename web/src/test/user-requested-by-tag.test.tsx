import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { UserRequestedByTag } from "@/components/user-detail/user-requested-by-tag";
import type { User, UserPatch } from "@/lib/types";

const { patchUser } = vi.hoisted(() => ({ patchUser: vi.fn() }));

vi.mock("@/lib/api", () => ({
  api: { patchUser: (id: number, patch: UserPatch) => patchUser(id, patch) },
  apiErrorMessage: (_error: unknown, fallback: string) => fallback,
}));

const USER = {
  id: 7,
  username: "kid",
  display_name: "Kid",
  slug: "kid",
  user_type: "managed",
  request_tag: "",
  requested_by_tag: "",
  prefs: {},
} as unknown as User;

function renderField(user: User = USER) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <UserRequestedByTag user={user} />
    </QueryClientProvider>,
  );
}

describe("UserRequestedByTag", () => {
  beforeEach(() => {
    patchUser.mockReset();
    patchUser.mockImplementation((_id: number, patch: UserPatch) =>
      Promise.resolve({ ...USER, ...patch }),
    );
  });

  it("labels the field for the tag THEIR requests carry, and says when to use it", () => {
    renderField();
    expect(
      screen.getByLabelText("Their request tag in Radarr/Sonarr"),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/doesn.t fit the row.s pattern, put it here, e\.g\. children/),
    ).toBeInTheDocument();
  });

  it("saves the tag on blur, as requested_by_tag and nothing else", async () => {
    renderField();
    const field = screen.getByLabelText("Their request tag in Radarr/Sonarr");
    await userEvent.type(field, "children");
    await userEvent.tab();
    await waitFor(() =>
      expect(patchUser).toHaveBeenCalledWith(7, {
        requested_by_tag: "children",
      }),
    );
    expect(await screen.findByText(/saved/i)).toBeInTheDocument();
  });

  it("does not PATCH when the value is unchanged", async () => {
    renderField({ ...USER, requested_by_tag: "children" });
    const field = screen.getByLabelText("Their request tag in Radarr/Sonarr");
    expect(field).toHaveValue("children");
    await userEvent.click(field);
    await userEvent.tab();
    expect(patchUser).not.toHaveBeenCalled();
  });
});
