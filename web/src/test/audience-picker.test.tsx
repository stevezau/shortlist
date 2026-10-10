import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { AudiencePicker } from "@/components/rows/audience-picker";
import type { CollectionInput, User } from "@/lib/types";
import { makeUser } from "@/test/user-fixtures";

function user(id: number, username: string): User {
  return makeUser({ id, username, slug: username.toLowerCase(), history_depth: 10 });
}

describe("AudiencePicker", () => {
  it("keeps the people list behind a disclosure and expands on click", async () => {
    render(
      <AudiencePicker
        audience="subset"
        audienceUserIds={[4]}
        users={[user(4, "sarah"), user(5, "mike")]}
        onChange={vi.fn()}
      />,
    );

    // Collapsed on open: a one-line summary, individual user toggles hidden.
    const summary = screen.getByRole("button", {
      name: /1 of 2 people chosen/i,
    });
    expect(screen.queryByLabelText("sarah")).toBeNull();

    await userEvent.click(summary);
    expect(screen.getByLabelText("sarah")).toBeTruthy();
    expect(screen.getByLabelText("mike")).toBeTruthy();
  });

  it("warns in the summary when nobody is chosen", () => {
    render(
      <AudiencePicker
        audience="subset"
        audienceUserIds={[]}
        users={[user(4, "sarah")]}
        onChange={vi.fn()}
      />,
    );
    expect(screen.getByText(/Nobody chosen/i)).toBeTruthy();
  });
});

const roster = Array.from({ length: 31 }, (_, index) => ({
  ...user(index + 1, `person${String(index + 1).padStart(2, "0")}`),
  display_name: `Viewer ${String(index + 1).padStart(2, "0")}`,
  enabled: index !== 30,
}));

function StatefulPicker({ onChange }: { onChange: (patch: Pick<CollectionInput, "audience" | "audience_user_ids">) => void }) {
  const [value, setValue] = useState<Pick<CollectionInput, "audience" | "audience_user_ids">>({
    audience: "everyone", audience_user_ids: [],
  });
  return <AudiencePicker audience={value.audience} audienceUserIds={value.audience_user_ids} users={roster} onChange={(next) => {
    setValue(next);
    onChange(next);
  }} />;
}

describe("audience search and pagination", () => {
  it("shows ten people by default in Everyone, and browsing never changes the audience", async () => {
    const onChange = vi.fn();
    render(<StatefulPicker onChange={onChange} />);
    const list = screen.getByRole("list", { name: "People" });
    expect(within(list).getAllByRole("listitem")).toHaveLength(10);
    expect(screen.getByRole("combobox", { name: "People per page" })).toHaveValue("10");
    expect(screen.getByRole("status")).toHaveTextContent("1–10 of 31 people");
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    expect(screen.queryByRole("switch")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByRole("status")).toHaveTextContent("11–20 of 31 people");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search people" }), "viewer 31");
    expect(screen.getByRole("status")).toHaveTextContent("1–1 of 1 matching people");
    expect(screen.getByText("Viewer 31")).toBeVisible();
    expect(screen.getByText(/Shortlist disabled/)).toBeVisible();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("searches usernames as well as display names, and empty results can clear the search", async () => {
    render(<StatefulPicker onChange={vi.fn()} />);
    const search = screen.getByRole("searchbox", { name: "Search people" });
    await userEvent.type(search, " PERSON25 ");
    expect(screen.getByText("Viewer 25")).toBeVisible();
    await userEvent.clear(search);
    await userEvent.type(search, "nobody matches");
    expect(screen.getByText("No people found")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("0 matching people");
    expect(screen.getByRole("button", { name: "Previous page" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(search).toHaveValue("");
    expect(search).toHaveFocus();
    expect(screen.getByRole("status")).toHaveTextContent("1–10 of 31 people");
  });

  it("offers10,25,50 and All, resets the page on a size change and bounds the final page", async () => {
    render(<StatefulPicker onChange={vi.fn()} />);
    const size = screen.getByRole("combobox", { name: "People per page" });
    expect(within(size).getAllByRole("option").map((option) => option.textContent)).toEqual(["10", "25", "50", "All"]);
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await userEvent.selectOptions(size, "25");
    expect(screen.getByRole("status")).toHaveTextContent("1–25 of 31 people");
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByRole("status")).toHaveTextContent("26–31 of 31 people");
    expect(screen.getByRole("button", { name: "Next page" })).toBeDisabled();
    await userEvent.selectOptions(size, "50");
    expect(within(screen.getByRole("list", { name: "People" })).getAllByRole("listitem")).toHaveLength(31);
    await userEvent.selectOptions(size, "all");
    expect(screen.getByText("Page 1 of 1")).toBeVisible();
  });

  it("clamps the page when the roster shrinks", async () => {
    const onChange = vi.fn();
    const props = { audience: "everyone" as const, audienceUserIds: [], users: roster, onChange };
    const { rerender } = render(<AudiencePicker {...props} />);
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    rerender(<AudiencePicker {...props} users={roster.slice(0, 3)} />);
    expect(screen.getByRole("status")).toHaveTextContent("1–3 of 3 people");
    expect(screen.getByText("Page 1 of 1")).toBeVisible();
    expect(onChange).not.toHaveBeenCalled();
  });

  it("preserves selections across pages, searches and an Everyone round trip", async () => {
    const onChange = vi.fn();
    render(<StatefulPicker onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: "Choose people" }));
    expect(onChange).toHaveBeenLastCalledWith({ audience: "subset", audience_user_ids: [] });
    await userEvent.click(screen.getByRole("switch", { name: "person01" }));
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await userEvent.click(screen.getByRole("switch", { name: "person11" }));
    await userEvent.type(screen.getByRole("searchbox", { name: "Search people" }), "person31");
    await userEvent.click(screen.getByRole("switch", { name: "person31" }));
    expect(onChange).toHaveBeenLastCalledWith({ audience: "subset", audience_user_ids: [1, 11, 31] });
    await userEvent.click(screen.getByRole("button", { name: "Everyone" }));
    expect(onChange).toHaveBeenLastCalledWith({ audience: "everyone", audience_user_ids: [1, 11, 31] });
    await userEvent.click(screen.getByRole("button", { name: "Choose people" }));
    await userEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByRole("switch", { name: "person01" })).toBeChecked();
    await userEvent.click(screen.getByRole("switch", { name: "person01" }));
    expect(onChange).toHaveBeenLastCalledWith({ audience: "subset", audience_user_ids: [11, 31] });
  });

  it("keeps selected IDs outside the current roster when one visible person is selected", async () => {
    const onChange = vi.fn();
    render(<AudiencePicker audience="subset" audienceUserIds={[999]} users={roster} onChange={onChange} />);
    await userEvent.click(screen.getByRole("button", { name: /1 of 31 people chosen/ }));
    await userEvent.click(screen.getByRole("switch", { name: "person01" }));
    expect(onChange).toHaveBeenCalledWith({ audience: "subset", audience_user_ids: [999, 1] });
  });

  it("explains an empty roster without a misleading search result", () => {
    render(<AudiencePicker audience="everyone" audienceUserIds={[]} users={[]} onChange={vi.fn()} />);
    expect(screen.getByText(/No users yet.*bring your Plex users/)).toBeVisible();
    expect(screen.queryByRole("button", { name: "Clear search" })).toBeNull();
  });
});
