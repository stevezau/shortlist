import { describe, expect, it } from "vitest";

import {
  autoSendBarsMatchGuardrails,
  describeRequestFlow,
} from "@/lib/request-flow";

const seerr = (over: Partial<Parameters<typeof describeRequestFlow>[0]> = {}) =>
  describeRequestFlow({
    viaSeerr: true,
    autoSend: true,
    everythingAutoSends: false,
    seerrApproves: "all",
    maxPerRun: 5,
    ...over,
  });

describe("describeRequestFlow", () => {
  describe("the Arr route", () => {
    it("keeps naming the apps, because it has only ever had one gate", () => {
      const on = describeRequestFlow({
        viaSeerr: false,
        autoSend: true,
        everythingAutoSends: false,
        seerrApproves: null,
            maxPerRun: 5,
      });
      expect(on.summary).toMatch(/Radarr or Sonarr/);
      expect(on.doubleApproval).toBeUndefined();

      const off = describeRequestFlow({
        viaSeerr: false,
        autoSend: false,
        everythingAutoSends: false,
        seerrApproves: null,
            maxPerRun: 5,
      });
      expect(off.summary).toMatch(/nothing is sent to Radarr or Sonarr/i);
      expect(off.doubleApproval).toBeUndefined();
    });
  });

  describe("the Overseerr route", () => {
    it("says a title starts downloading when the account approves automatically", () => {
      expect(seerr({ seerrApproves: "all" }).summary).toMatch(
        /approved there automatically, so they start downloading/,
      );
    });

    it("says a title is filed for approval when the account cannot approve", () => {
      expect(seerr({ seerrApproves: "none" }).summary).toMatch(
        /filed in Overseerr for you to approve there/,
      );
    });

    it("claims nothing about approval while the account list is unknown", () => {
      // Unreachable instance, or still loading. Saying "starts downloading" here would be a guess,
      // and the whole point of this sentence is that it is always true.
      const unknown = seerr({ seerrApproves: null });
      expect(unknown.summary).toMatch(/go to Overseerr/);
      expect(unknown.summary).not.toMatch(/automatically|approve there/);
      expect(unknown.doubleApproval).toBeUndefined();
    });

    it("spells out the undiscoverable setup when the bars match the guardrails", () => {
      const flow = seerr({ everythingAutoSends: true, maxPerRun: 5 });
      // "…up to 5 a night", NOT "nothing waits here": `max_per_run` queues the overflow whatever
      // the bars say. Measured against the real engine, 12 qualifying titles at the default cap of
      // 5 left seven waiting, under a sentence promising the inbox would be empty.
      expect(flow.summary).toMatch(/up to 5 a night/);
      expect(flow.summary).toMatch(/Anything past that waits here/);
      expect(flow.summary).not.toMatch(/Nothing waits here/);
      expect(flow.doubleApproval).toBeUndefined();
    });

    it("carries the owner's own cap into that sentence", () => {
      expect(seerr({ everythingAutoSends: true, maxPerRun: 3 }).summary).toMatch(
        /up to 3 a night/,
      );
    });

    it("describes a part-approving account without contradicting the card", () => {
      // The card says "films will be approved automatically, while shows wait". Collapsing that to
      // a boolean made this sentence say everything was filed for review — one screen disagreeing
      // with itself, and the double-approval warning firing for films that never wait.
      const flow = seerr({ seerrApproves: "partial" });
      expect(flow.summary).toMatch(
        /films are approved automatically and shows wait for your yes/,
      );
      expect(flow.doubleApproval).toBeUndefined();
    });
  });

  describe("approving the same title twice", () => {
    it("names it when auto-send is off and the account cannot approve", () => {
      const flow = seerr({ autoSend: false, seerrApproves: "none" });
      expect(flow.doubleApproval).toMatch(/twice/);
      expect(flow.doubleApproval).toMatch(
        /match the bars|approves automatically/,
      );
    });

    it("names it for the lower tier when only the strongest are sent", () => {
      const flow = seerr({ autoSend: true, seerrApproves: "none" });
      expect(flow.doubleApproval).toMatch(/approving again in Overseerr/);
    });

    it("stays quiet when there is genuinely only one gate", () => {
      // Every combination that does NOT make you approve twice.
      expect(seerr({ seerrApproves: "all" }).doubleApproval).toBeUndefined();
      expect(
        seerr({ autoSend: false, seerrApproves: "all" }).doubleApproval,
      ).toBeUndefined();
      expect(
        seerr({ everythingAutoSends: true, seerrApproves: "none" })
          .doubleApproval,
      ).toBeUndefined();
    });

    it("never fires on the Arr route, which has no second queue", () => {
      for (const autoSend of [true, false]) {
        expect(
          describeRequestFlow({
            viaSeerr: false,
            autoSend,
            everythingAutoSends: false,
            seerrApproves: "none",
            maxPerRun: 5,
          }).doubleApproval,
        ).toBeUndefined();
      }
    });
  });

  it("agrees in number with the subject it is completing", () => {
    // "Titles that clear the higher bars below GOES to Overseerr and IS approved" — one shared
    // phrase used after both a plural and a singular subject. Wrong in the commonest setup there is.
    const plural = seerr({ seerrApproves: "all" }).summary;
    expect(plural).toMatch(/Titles that clear the higher bars below go to/);
    expect(plural).not.toMatch(/below goes|and is approved|so it starts/);

    const singular = seerr({ autoSend: false, seerrApproves: "all" }).summary;
    expect(singular).toMatch(/it goes to Overseerr and is approved/);
    expect(singular).not.toMatch(/it go to|are approved/);
  });

  it("always produces a sentence, for every combination of inputs", () => {
    // The summary is load-bearing: an empty one would leave the section saying nothing at all.
    for (const viaSeerr of [true, false])
      for (const autoSend of [true, false])
        for (const everythingAutoSends of [true, false])
          for (const seerrApproves of [
            "all",
            "none",
            "partial",
            null,
          ] as const) {
            const flow = describeRequestFlow({
              viaSeerr,
              autoSend,
              everythingAutoSends,
              seerrApproves,
              maxPerRun: 5,
            });
            expect(flow.summary.length).toBeGreaterThan(20);
            expect(flow.summary).toMatch(/\.$/);
          }
  });
});

describe("autoSendBarsMatchGuardrails", () => {
  it("is true when the bars sit exactly on the guardrails", () => {
    expect(
      autoSendBarsMatchGuardrails({
        autoMinDemand: 2,
        autoMinRating: 7.3,
        minDemand: 2,
        minRating: 7.3,
      }),
    ).toBe(true);
  });

  it("is true when a bar is BELOW the guardrail, which is just as inert", () => {
    // An owner who dragged it to zero meaning "send everything" must not be told their titles wait
    // somewhere they do not.
    expect(
      autoSendBarsMatchGuardrails({
        autoMinDemand: 0,
        autoMinRating: 0,
        minDemand: 2,
        minRating: 7.3,
      }),
    ).toBe(true);
  });

  it("is false while either bar is still higher", () => {
    expect(
      autoSendBarsMatchGuardrails({
        autoMinDemand: 4,
        autoMinRating: 7.3,
        minDemand: 2,
        minRating: 7.3,
      }),
    ).toBe(false);
    expect(
      autoSendBarsMatchGuardrails({
        autoMinDemand: 2,
        autoMinRating: 7.5,
        minDemand: 2,
        minRating: 7.3,
      }),
    ).toBe(false);
  });
});
