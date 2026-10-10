import { describe, expect, it } from "vitest";

import { accountHasProblem, accountSummary, gridCell, rowColumnName } from "@/lib/privacy-grid";
import type { AccountPrivacy, PrivacyStatus } from "@/lib/types";

function account(overrides: Partial<AccountPrivacy> = {}): AccountPrivacy {
  return {
    user: "sarah",
    display_name: "Sarah",
    slug: "sarah",
    account_id: 1,
    user_id: 7,
    user_type: "shared",
    restriction_profile: "",
    manage_sharing: true,
    state: "hiding",
    hides: ["shortlist_mike"],
    should_hide: ["shortlist_mike"],
    missing: [],
    other_conditions: [],
    ...overrides,
  };
}

function status(overrides: Partial<PrivacyStatus["enforcement"]> = {}): PrivacyStatus {
  return {
    read_at: "2026-09-05T02:00:00+00:00",
    summary: "clean",
    accounts: [],
    rows_on_plex: [],
    snapshots_kept: 0,
    rows_error: null,
    error: null,
    enforcement: {
      measured: false,
      run_id: null,
      measured_at: null,
      not_enforced: {},
      unhideable: {},
      unhideable_measured: false,
      unhideable_run_id: null,
      unhideable_measured_at: null,
      ...overrides,
    },
  };
}

describe("gridCell", () => {
  it("reads a stored exclude as hidden and the account's own label as own", () => {
    expect(gridCell(account(), "shortlist_mike", status())).toBe("hidden");
    expect(gridCell(account(), "shortlist_sarah", status())).toBe("own");
  });

  it("reads a missing exclude as 'sees'", () => {
    const a = account({ state: "missing", hides: [], missing: ["shortlist_mike"] });
    expect(gridCell(a, "shortlist_mike", status())).toBe("sees");
  });

  it("does not call a stored exclude hidden when a run saw Plex ignoring it", () => {
    const s = status({ measured: true, not_enforced: { sarah: [11] } });
    expect(gridCell(account(), "shortlist_mike", s)).toBe("stored_not_applied");
  });

  it("does not call an unmeasured account of the same kind hidden when a run saw Plex ignoring the rule", () => {
    const s = status({ measured: true, not_enforced: { sarah: [21] } });
    s.accounts = [account(), account({ user: "bob", slug: "bob", user_id: 8 })];
    const bob = s.accounts[1]!;
    expect(gridCell(bob, "shortlist_mike", s)).toBe("stored_not_applied");
  });

  it("leaves an account of a different kind hidden", () => {
    const s = status({ measured: true, not_enforced: { sarah: [21] } });
    s.accounts = [account(), account({ user: "kid", slug: "kid", user_id: 9, user_type: "managed" })];
    expect(gridCell(s.accounts[1]!, "shortlist_mike", s)).toBe("hidden");
  });

  it("covers every non-owner account when the flagged name is on no account", () => {
    const s = status({ measured: true, not_enforced: { ghost: [21] } });
    s.accounts = [account({ user_type: "managed" })];
    expect(gridCell(s.accounts[0]!, "shortlist_mike", s)).toBe("stored_not_applied");
  });

  it("does not claim a profiled account sees a row a run found it could not see", () => {
    const a = account({ state: "refused_by_plex", hides: [], missing: ["shortlist_mike"], restriction_profile: "little_kid" });
    expect(gridCell(a, "shortlist_mike", status({ unhideable_measured: true }))).toBe("refused");
    expect(gridCell(a, "shortlist_mike", status())).toBe("sees");
  });

  it("answers for the owner, a left-alone account and an unread one without looking at filters", () => {
    expect(gridCell(account({ state: "owner" }), "shortlist_mike", status())).toBe("owner");
    expect(gridCell(account({ state: "left_alone" }), "shortlist_mike", status())).toBe("left_alone");
    expect(gridCell(account({ state: "unknown" }), "shortlist_mike", status())).toBe("unknown");
  });
});

describe("rowColumnName", () => {
  it("uses the display name of the account the label belongs to, else the slug", () => {
    expect(rowColumnName("shortlist_sarah", [account()])).toBe("Sarah’s rows");
    expect(rowColumnName("shortlist_mike", [account()])).toBe("mike’s rows");
  });
});

describe("accountSummary", () => {
  const rows = ["shortlist_mike", "shortlist_dan", "shortlist_sarah"];

  it("counts a clean account's other rows as hidden and its own as own", () => {
    const s = { ...status(), rows_on_plex: rows };
    const a = account({ hides: ["shortlist_mike", "shortlist_dan"] });
    expect(accountSummary(a, s)).toMatchObject({ hidden: 2, own: 1, sees: 0, unknown: 0 });
    expect(accountHasProblem(a, s)).toBe(false);
  });

  it("counts the rows an account sees", () => {
    const s = { ...status(), rows_on_plex: rows };
    const a = account({ state: "missing", hides: [], missing: ["shortlist_mike", "shortlist_dan"] });
    expect(accountSummary(a, s)).toMatchObject({ sees: 2, hidden: 0, own: 1 });
    expect(accountHasProblem(a, s)).toBe(true);
  });

  it("counts a row in neither list as unknown, never hidden", () => {
    const s = { ...status(), rows_on_plex: rows };
    const a = account({ hides: ["shortlist_mike"] });
    expect(accountSummary(a, s)).toMatchObject({ hidden: 1, unknown: 1 });
  });

  it("counts stored-but-ignored rules as a problem", () => {
    const s = { ...status({ measured: true, not_enforced: { sarah: [1] } }), rows_on_plex: rows };
    const a = account({ hides: ["shortlist_mike", "shortlist_dan"] });
    expect(accountSummary(a, s).stored_not_applied).toBe(2);
    expect(accountHasProblem(a, s)).toBe(true);
  });

  it("flags an unreadable filter even with no rows to count", () => {
    expect(accountHasProblem(account({ state: "unreadable_filter" }), status())).toBe(true);
  });

  it("does not call the owner or a left-alone account a problem", () => {
    const s = { ...status(), rows_on_plex: rows };
    expect(accountHasProblem(account({ state: "owner" }), s)).toBe(false);
    expect(accountHasProblem(account({ state: "left_alone" }), s)).toBe(false);
  });
});
