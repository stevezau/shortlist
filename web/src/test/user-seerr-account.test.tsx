import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type * as ApiModule from "@/lib/api";
import { ApiError } from "@/lib/api";
import { UserSeerrAccount } from "@/components/user-detail/user-seerr-account";
import type { SeerrOptions, User, UserPatch } from "@/lib/types";
import { makeUser } from "@/test/user-fixtures";

const { getSeerrOptions, getUsers, getSettings, patchUser } = vi.hoisted(() => ({
  getSeerrOptions: vi.fn(),
  getUsers: vi.fn(),
  getSettings: vi.fn(),
  patchUser: vi.fn(),
}));

vi.mock("@/lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof ApiModule>();
  return {
    ...actual,
    api: {
      getSeerrOptions: () => getSeerrOptions(),
      getUsers: () => getUsers(),
      getSettings: () => getSettings(),
      patchUser: (id: number, patch: UserPatch) => patchUser(id, patch),
    },
  };
});

const account = (id: number, name: string, plex_id: number | null) => ({
  id,
  name,
  plex_id,
  auto_approve_movies: false,
  auto_approve_tv: false,
  is_plex_user: plex_id !== null,
});

const OPTIONS: SeerrOptions = {
  default_user_id: 1,
  users: [account(1, "admin", 900), account(12, "kitchen", null), account(30, "mike-signin", 777)],
};

const SARAH: User = makeUser({ id: 4, username: "sarah", display_name: "Sarah", plex_account_id: 555 });
const MIKE: User = makeUser({ id: 5, username: "mike", display_name: "Mike", plex_account_id: 777 });

function renderPicker(user: User = SARAH) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <UserSeerrAccount user={user} />
    </QueryClientProvider>,
  );
}

describe("UserSeerrAccount", () => {
  beforeEach(() => {
    getSeerrOptions.mockReset();
    getSeerrOptions.mockResolvedValue(OPTIONS);
    getUsers.mockReset();
    getUsers.mockResolvedValue([SARAH, MIKE]);
    getSettings.mockReset();
    getSettings.mockResolvedValue({});
    patchUser.mockReset();
    patchUser.mockImplementation((_id: number, patch: UserPatch) => Promise.resolve({ ...SARAH, ...patch }));
  });

  it("renders nothing when Overseerr isn't connected", async () => {
    getSeerrOptions.mockRejectedValue(new ApiError(409, "Connect Overseerr first"));
    const { container } = renderPicker();
    await waitFor(() => expect(getSeerrOptions).toHaveBeenCalled());
    await waitFor(() => expect(screen.queryByTestId("seerr-account-loading")).toBeNull());
    expect(container).toBeEmptyDOMElement();
  });

  it("shows a message and a retry when Overseerr can't be read", async () => {
    getSeerrOptions.mockRejectedValue(new ApiError(502, "Overseerr unreachable"));
    renderPicker();
    expect(await screen.findByRole("alert")).toHaveTextContent("Overseerr unreachable");
    expect(screen.getByRole("button", { name: "Try again" })).toBeInTheDocument();
  });

  it("says when Overseerr has no accounts to choose from", async () => {
    getSeerrOptions.mockResolvedValue({ default_user_id: null, users: [] });
    renderPicker();
    expect(await screen.findByText(/no accounts to choose from/)).toBeInTheDocument();
  });

  it("is one quiet line for someone linked by their Plex sign-in, with Change to open the picker", async () => {
    renderPicker(MIKE);
    expect(await screen.findByText("Linked automatically as mike-signin")).toBeInTheDocument();
    expect(screen.queryByRole("combobox")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Change" }));

    expect(screen.getByRole("combobox")).toBeInTheDocument();
  });

  it("opens the picker straight away for someone nothing links, marking accounts other people sign in as", async () => {
    renderPicker();
    const select = await screen.findByRole("combobox", { name: "Their Overseerr account" });
    expect(select).toHaveValue("0");
    expect(screen.getByRole("option", { name: "Automatic (their Plex sign-in)" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "kitchen" })).toBeInTheDocument();
    expect(screen.getByRole("option", { name: "mike-signin (signs in as Mike)" })).toBeInTheDocument();
  });

  it("opens the picker when an account was chosen, and saves a new choice as seerr_user_id", async () => {
    renderPicker({ ...MIKE, seerr_user_id: 12 });
    const select = await screen.findByRole("combobox");
    expect(select).toHaveValue("12");

    await userEvent.selectOptions(select, "30");

    await waitFor(() => expect(patchUser).toHaveBeenCalledWith(5, { seerr_user_id: 30 }));
    expect(await screen.findByText(/^saved$/i)).toBeInTheDocument();
  });

  it("shows the server's reason when another person already holds the account", async () => {
    patchUser.mockRejectedValue(new ApiError(409, "That Overseerr account is already linked to Mike."));
    renderPicker();
    await userEvent.selectOptions(await screen.findByRole("combobox"), "30");
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "That Overseerr account is already linked to Mike.",
    );
  });

  it("warns when the chosen account is the one Shortlist files its own requests as", async () => {
    getSettings.mockResolvedValue({ "requests.overseerr.request_as_user_id": 12 });
    renderPicker({ ...SARAH, seerr_user_id: 12 });
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Shortlist files its own requests as this account, so none of its requests count as theirs.",
    );
  });

  it("carries its own heading, so nothing is left over when Overseerr isn't connected", async () => {
    renderPicker();
    expect(await screen.findByRole("heading", { name: "Overseerr account" })).toBeInTheDocument();
  });

  it("keeps showing a chosen account Overseerr no longer has, instead of reading as Automatic", async () => {
    renderPicker({ ...SARAH, seerr_user_id: 99 });
    const select = await screen.findByRole("combobox");
    expect(select).toHaveValue("99");
    expect(screen.getByRole("option", { name: "Account 99 (no longer in Overseerr)" })).toBeInTheDocument();
  });

  it("does not call someone linked when their account was handed to another person", async () => {
    const profile = makeUser({ id: 6, username: "profile", display_name: "Profile", plex_account_id: 888, seerr_user_id: 30 });
    getUsers.mockResolvedValue([SARAH, MIKE, profile]);
    renderPicker(MIKE);
    expect(await screen.findByText("mike-signin is linked to Profile instead")).toBeInTheDocument();
    expect(screen.queryByText(/Linked automatically/)).toBeNull();
    expect(screen.getByRole("combobox")).toHaveValue("0");
  });

  it("marks an account chosen for another person, so picking it isn't a surprise refusal", async () => {
    const profile = makeUser({ id: 6, username: "profile", display_name: "Profile", plex_account_id: 888, seerr_user_id: 12 });
    getUsers.mockResolvedValue([SARAH, MIKE, profile]);
    renderPicker();
    expect(await screen.findByRole("option", { name: "kitchen (linked to Profile)" })).toBeInTheDocument();
  });

  it("does not warn for any other account", async () => {
    getSettings.mockResolvedValue({ "requests.overseerr.request_as_user_id": 1 });
    renderPicker({ ...SARAH, seerr_user_id: 12 });
    await screen.findByRole("combobox");
    expect(screen.queryByRole("alert")).toBeNull();
  });
});
