import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { MemoryRouter } from "react-router";
import { describe, expect, it, vi } from "vitest";

import { AudiencePicker } from "@/components/rows/audience-picker";
import { RowAudienceTable } from "@/components/rows/row-audience-table";
import type { CollectionInput, User } from "@/lib/types";

const users: User[] = Array.from({ length: 35 }, (_, index) => ({
  id: index + 1, username: `person${String(index + 1).padStart(2, "0")}`, display_name: `Viewer ${index + 1}`,
  slug: `person${index + 1}`, user_type: index === 32 ? "owner" : "shared", enabled: index !== 33,
  prefs: index === 34 ? { paused: true } : {}, manage_sharing: true, restricted: false,
  cold_start: false, history_depth: 10, last_run_at: null, request_tag: "", requested_by_tag: "",
  picks_watched_30d: null, last_pick_watched_at: null, nickname: "", friendly_name: "", avatar_url: "",
  plex_account_id: index + 1, restriction_profile: "", unhidden_rows: 0, departed: false, preview_titles: [],
}));

describe("the actual audience table", () => {
  it("pages the reached people in Everyone and keeps the real library/privacy columns", async () => {
    render(<MemoryRouter><RowAudienceTable input={{ audience: "everyone", audience_user_ids: [] }} users={users} libraries={null} accounts={[]} privacy="error" /></MemoryRouter>);
    const table = screen.getByRole("table", { name: "People" });
    expect(within(table).getAllByRole("row")).toHaveLength(11);
    expect(screen.getByRole("status")).toHaveTextContent("1–10 of 32 people");
    expect(within(table).getByRole("columnheader", { name: "Gets a copy in" })).toBeInTheDocument();
    expect(within(table).getAllByText("Not checked")).toHaveLength(10);
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    expect(screen.getByRole("status")).toHaveTextContent("11–20 of 32 people");
    await userEvent.type(screen.getByRole("searchbox", { name: "Search people" }), "person32");
    expect(within(table).getAllByRole("row")).toHaveLength(2);
    expect(screen.getByRole("status")).toHaveTextContent("1–1 of 1 matching people");
    expect(screen.getByText(/Plex can.t restrict the owner/)).toBeVisible();
  });

  it("combines selection and the real table into one searchable roster without dropping disabled users", async () => {
    const onChange = vi.fn();
    function EditorAudience() {
      const [input, setInput] = useState<Pick<CollectionInput, "audience" | "audience_user_ids">>({ audience: "everyone", audience_user_ids: [] });
      return <AudiencePicker audience={input.audience} audienceUserIds={input.audience_user_ids} users={users} onChange={setInput}>
        <RowAudienceTable input={input} users={users} libraries={null} accounts={[]} privacy="loading" onSelect={(id, checked) => {
          const next = { audience: "subset" as const, audience_user_ids: checked ? [...input.audience_user_ids, id] : input.audience_user_ids.filter((value) => value !== id) };
          setInput(next);
          onChange(next);
        }} />
      </AudiencePicker>;
    }
    render(<MemoryRouter><EditorAudience /></MemoryRouter>);
    await userEvent.click(screen.getByRole("button", { name: "Choose people" }));
    expect(screen.getAllByRole("searchbox")).toHaveLength(1);
    expect(screen.getAllByRole("table")).toHaveLength(1);
    await userEvent.click(screen.getByRole("switch", { name: "person01" }));
    await userEvent.click(screen.getByRole("button", { name: "Next page" }));
    await userEvent.click(screen.getByRole("switch", { name: "person11" }));
    await userEvent.type(screen.getByRole("searchbox", { name: "Search people" }), "person34");
    expect(screen.getAllByText("Shortlist disabled").length).toBeGreaterThan(0);
    await userEvent.click(screen.getByRole("switch", { name: "person34" }));
    expect(onChange).toHaveBeenLastCalledWith({ audience: "subset", audience_user_ids: [1, 11, 34] });
    await userEvent.click(screen.getByRole("button", { name: "Clear search" }));
    expect(screen.getByRole("switch", { name: "person01" })).toBeChecked();
  });
});
