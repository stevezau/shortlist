/**
 * When the rail's Privacy item carries a warning dot.
 *
 * The dot only REPORTS what the privacy endpoint already decided (`api/privacy.py` owns the states
 * and the headline). It shows when the headline is anything but clean, and when any account is in a
 * state where it is not hiding other people's rows — including `refused_by_plex`, which the headline
 * can leave "clean" when no run has measured it. It never shows for an account the owner chose to
 * leave alone, or for the owner's own account: those are settings and Plex facts, not faults.
 */

import { describe, expect, it } from "vitest";

import { privacyGlance, privacyNeedsAttention, rowsNotHidden, rowsNotTheirs } from "@/lib/privacy-attention";
import type { AccountPrivacy, PrivacyStatus } from "@/lib/types";

function account(state: string): AccountPrivacy {
  return {
    account_id: 1,
    display_name: "Sarah",
    hides: [],
    manage_sharing: state !== "left_alone",
    missing: [],
    other_conditions: [],
    restriction_profile: state === "refused_by_plex" ? "older_kid" : "",
    should_hide: [],
    slug: "sarah",
    state,
    user: "sarah",
    user_id: 1,
    user_type: "shared",
  };
}

function status(summary: string, states: string[]): PrivacyStatus {
  return {
    accounts: states.map(account),
    enforcement: {} as PrivacyStatus["enforcement"],
    error: null,
    read_at: "2026-10-03T02:30:00Z",
    rows_error: null,
    rows_on_plex: [],
    snapshots_kept: 0,
    summary,
  };
}

describe("privacyNeedsAttention", () => {
  it("stays quiet before the first answer arrives", () => {
    expect(privacyNeedsAttention(undefined)).toBe(false);
  });

  it("stays quiet when every account hides every row", () => {
    expect(privacyNeedsAttention(status("clean", ["hiding", "hiding", "owner"]))).toBe(false);
  });

  it("stays quiet for an account the owner chose to leave alone", () => {
    expect(privacyNeedsAttention(status("clean", ["hiding", "left_alone"]))).toBe(false);
  });

  it("stays quiet on a server with nobody to hide anything from", () => {
    expect(privacyNeedsAttention(status("clean", []))).toBe(false);
  });

  it.each(["missing", "unreadable_filter", "refused_by_plex"])(
    "warns when an account is %s, even under a clean headline",
    (state) => {
      expect(privacyNeedsAttention(status("clean", ["hiding", state]))).toBe(true);
    },
  );

  it.each(["unreadable", "rows_unknown", "missing", "filter_unreadable", "not_enforced", "unhideable"])(
    "warns when the headline is %s",
    (summary) => {
      expect(privacyNeedsAttention(status(summary, ["hiding"]))).toBe(true);
    },
  );
});

/**
 * ONE figure for "rows that aren't theirs", on every screen that says it.
 *
 * The Dashboard read the live `missing` list (3), Users read the run's `unhidden_rows` (5) and Privacy
 * printed "Hides 0 of 3 rows" — three screens, two units. The run measures COLLECTIONS (one per row
 * per library); the Privacy page counts ROWS (one per person, however many libraries it spans). Rows
 * won, because that reading is live and is the one the Privacy page prints.
 */
describe("rowsNotHidden", () => {
  const OTHERS = ["shortlist_sarah", "shortlist_mike", "shortlist_jess"];
  const kid = (): AccountPrivacy => ({ ...account("refused_by_plex"), user: "kid", should_hide: OTHERS, missing: OTHERS });
  const withEnforcement = (enforcement: Partial<PrivacyStatus["enforcement"]>): PrivacyStatus => ({
    ...status("unhideable", []),
    enforcement: enforcement as PrivacyStatus["enforcement"],
  });

  it("counts rows, the Privacy page's unit, never the run's per-library collections", () => {
    const measured = withEnforcement({ unhideable_measured: true, unhideable: { kid: [11, 12, 13, 14, 15] } });

    expect(rowsNotHidden(kid(), measured)).toBe(3);
  });

  it("is the gap in the Privacy page's 'Hides X of Y rows'", () => {
    const short = { ...account("missing"), should_hide: ["a", "b", "c"], hides: ["a"], missing: ["b", "c"] };

    expect(rowsNotHidden(short, status("missing", []))).toBe(short.should_hide.length - short.hides.length);
  });

  it("counts a profiled account no run has looked through yet", () => {
    expect(rowsNotHidden(kid(), withEnforcement({}))).toBe(3);
  });

  it("is zero for a profiled account a run looked through and saw nobody else's rows (little_kid)", () => {
    const lookedAndSawNone = withEnforcement({ unhideable_measured: true, unhideable: {} });

    expect(rowsNotHidden(kid(), lookedAndSawNone)).toBe(0);
  });

  it("matches the run's username without regard to case", () => {
    const measured = withEnforcement({ unhideable_measured: true, unhideable: { Kid: [11] } });

    expect(rowsNotHidden(kid(), measured)).toBe(3);
  });

  // Not exposure: a clean filter, Plex's own limit on the owner, the owner's own choice, and a reading
  // with no rows to check against. Each must stay silent even with labels in `missing`.
  it.each(["hiding", "owner", "left_alone", "unknown"])("is zero for an account that is %s", (state) => {
    expect(rowsNotHidden({ ...account(state), missing: OTHERS }, status("clean", []))).toBe(0);
  });
});

describe("rowsNotTheirs", () => {
  it("says the count in the words every screen uses", () => {
    expect(rowsNotTheirs(3)).toBe("can see 3 rows that aren’t theirs");
  });

  it("says one row in the singular", () => {
    expect(rowsNotTheirs(1)).toBe("can see 1 row that isn’t theirs");
  });
});

describe("privacyGlance with a run that saw Plex ignoring the rules", () => {
  it("does not count an unmeasured account of the flagged kind as private", () => {
    const s = status("not_enforced", ["hiding", "hiding"]);
    s.accounts[1] = { ...account("hiding"), user: "bob", slug: "bob", user_id: 2 };
    s.rows_on_plex = ["shortlist_sarah"];
    s.enforcement = { measured: true, not_enforced: { sarah: [21] } } as unknown as PrivacyStatus["enforcement"];
    const glance = privacyGlance(s);
    expect(glance).toMatchObject({ kind: "counted", hiding: 0, total: 2 });
  });
});
