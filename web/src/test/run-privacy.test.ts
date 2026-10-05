/**
 * What a run's privacy measurement lets the page claim.
 *
 * The run REPORTS what it measured (`api/runs.py::_run_privacy`); these helpers only decide which
 * sentence that licenses. The rule they protect: an unmeasured check is never an all-clear. `privacy`
 * is null when the run did not measure at all (older runs, dry runs, runs that died early), and a
 * list is null when that one check did not run — neither may read as "every row is hidden".
 */
import { describe, expect, it } from "vitest";

import {
  hasPrivacyWarning,
  nameList,
  privacyFindings,
  runPrivacyVerdict,
  type RunPrivacy,
} from "@/lib/run-privacy";

function privacy(overrides: Partial<RunPrivacy> = {}): RunPrivacy {
  return {
    can_see_others: [],
    unreadable_filters: [],
    filters_not_enforced: [],
    unchecked: [],
    write_failed: [],
    left_alone: [],
    ...overrides,
  };
}

describe("privacyFindings", () => {
  it("names every flagged account once, across all three checks", () => {
    const findings = privacyFindings(
      privacy({ can_see_others: ["kid"], unreadable_filters: ["Mike"], filters_not_enforced: ["mike", "jess"] }),
    );

    expect(findings).toEqual(["kid", "Mike", "jess"]);
  });

  it("reads a check that did not run as no finding, not as an error", () => {
    expect(privacyFindings(privacy({ unreadable_filters: null, filters_not_enforced: null }))).toEqual([]);
  });

  it("has nothing to say about a run that did not measure", () => {
    expect(privacyFindings(null)).toEqual([]);
    expect(privacyFindings(undefined)).toEqual([]);
  });
});

describe("hasPrivacyWarning", () => {
  it("warns on an OK run that flagged an account", () => {
    expect(hasPrivacyWarning({ status: "ok", privacy: privacy({ can_see_others: ["kid"] }) })).toBe(true);
  });

  it("stays plain OK when nothing was flagged", () => {
    expect(hasPrivacyWarning({ status: "ok", privacy: privacy() })).toBe(false);
  });

  it("stays plain OK when the run did not measure", () => {
    expect(hasPrivacyWarning({ status: "ok", privacy: null })).toBe(false);
  });

  it("stays plain OK when the only checks that ran were clean and the rest did not run", () => {
    expect(
      hasPrivacyWarning({
        status: "ok",
        privacy: privacy({ can_see_others: [], unreadable_filters: null, filters_not_enforced: null }),
      }),
    ).toBe(false);
  });

  it("leaves a failed run's status alone — 'with warnings' is only ever said of an OK run", () => {
    expect(hasPrivacyWarning({ status: "error", privacy: privacy({ can_see_others: ["kid"] }) })).toBe(false);
  });
});

describe("runPrivacyVerdict", () => {
  const people = ["sarah", "mike", "jess", "kid"];

  it("counts the accounts that hide every row when one can see others", () => {
    expect(runPrivacyVerdict(privacy({ can_see_others: ["kid"] }), people)).toEqual({
      kind: "counted",
      hiding: 3,
      total: 4,
      flagged: ["kid"],
      enforcementChecked: true,
    });
  });

  it("says not measured when the run did not measure", () => {
    expect(runPrivacyVerdict(null, people)).toEqual({ kind: "not_measured" });
  });

  it("will not count every account as hiding when Plex's filter read did not run", () => {
    expect(runPrivacyVerdict(privacy({ unreadable_filters: null }), people)).toEqual({
      kind: "partly_measured",
      flagged: [],
    });
  });

  it("still counts when only the enforcement spot-check did not run, and says so", () => {
    expect(runPrivacyVerdict(privacy({ filters_not_enforced: null }), people)).toEqual({
      kind: "counted",
      hiding: 4,
      total: 4,
      flagged: [],
      enforcementChecked: false,
    });
  });

  it("counts a flagged account that was not one of the run's people rather than claiming 4 of 4", () => {
    // The privacy merge writes to EVERY account on the server, not only the people this run built
    // for, so an unreadable filter can belong to someone outside `users`.
    expect(runPrivacyVerdict(privacy({ unreadable_filters: ["guest"] }), people)).toMatchObject({
      kind: "counted",
      hiding: 4,
      total: 5,
    });
  });

  it("matches names case-insensitively, so one account is never counted twice", () => {
    expect(runPrivacyVerdict(privacy({ can_see_others: ["KID"] }), people)).toMatchObject({
      hiding: 3,
      total: 4,
    });
  });

  it("has no 0-of-0 claim to make when there are no accounts at all", () => {
    expect(runPrivacyVerdict(privacy(), [])).toEqual({ kind: "no_accounts" });
  });
});

describe("runPrivacyVerdict, for accounts the run could not vouch for", () => {
  const people = ["sarah", "mike", "jess", "kid"];

  it("does not count a profiled account nobody could look through as hiding", () => {
    // The finding: a PIN-protected kid account with a Restriction Profile, which the engine logs as
    // "reports nothing rather than a false all-clear", read green "2 of 2 accounts hide every row".
    expect(runPrivacyVerdict(privacy({ unchecked: ["kid"] }), people)).toEqual({
      kind: "unvouched",
      hiding: 3,
      total: 4,
      flagged: [],
      unchecked: ["kid"],
      writeFailed: [],
      leftAlone: [],
    });
  });

  it("does not count an account whose filter write failed as hiding", () => {
    expect(runPrivacyVerdict(privacy({ write_failed: ["mike"] }), people)).toMatchObject({
      kind: "unvouched",
      hiding: 3,
      total: 4,
      writeFailed: ["mike"],
    });
  });

  it("does not count an account left alone as hiding — it sees every row, by the owner's choice", () => {
    expect(runPrivacyVerdict(privacy({ left_alone: ["jess"] }), people)).toMatchObject({
      kind: "unvouched",
      hiding: 3,
      total: 4,
      leftAlone: ["jess"],
    });
  });

  it("counts such an account that was not one of the run's people rather than claiming 4 of 4", () => {
    expect(runPrivacyVerdict(privacy({ left_alone: ["guest"] }), people)).toMatchObject({
      kind: "unvouched",
      hiding: 4,
      total: 5,
    });
  });

  it("counts an account once however many lists name it, matching case-insensitively", () => {
    const verdict = runPrivacyVerdict(
      privacy({ unreadable_filters: ["Mike"], write_failed: ["mike"], unchecked: ["KID"] }),
      people,
    );

    expect(verdict).toEqual({
      kind: "unvouched",
      hiding: 2,
      total: 4,
      flagged: ["Mike"],
      unchecked: ["KID"],
      writeFailed: [],
      leftAlone: [],
    });
  });

  it.each(["unchecked", "write_failed", "left_alone"] as const)(
    "will not count every account as hiding on a run that did not record %s",
    (key) => {
      // An older run's silence is "not recorded", never "nobody": it is exactly the run this finding was
      // about, so it may not keep its all-clear.
      expect(runPrivacyVerdict(privacy({ [key]: null }), people)).toEqual({ kind: "partly_measured", flagged: [] });
    },
  );
});

describe("nameList", () => {
  it("joins names as a sentence", () => {
    expect(nameList(["kid"])).toBe("kid");
    expect(nameList(["kid", "tom"])).toBe("kid and tom");
    expect(nameList(["a", "b", "c"])).toBe("a, b and c");
  });

  it("folds a long list into a count", () => {
    expect(nameList(["a", "b", "c", "d", "e"])).toBe("a, b, c and 2 more");
  });
});
