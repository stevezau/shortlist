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
    seerrApproves: true,
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
      });
      expect(on.summary).toMatch(/Radarr or Sonarr/);
      expect(on.doubleApproval).toBeUndefined();

      const off = describeRequestFlow({
        viaSeerr: false,
        autoSend: false,
        everythingAutoSends: false,
        seerrApproves: null,
      });
      expect(off.summary).toMatch(/nothing is sent to Radarr or Sonarr/i);
      expect(off.doubleApproval).toBeUndefined();
    });
  });

  describe("the Overseerr route", () => {
    it("says a title starts downloading when the account approves automatically", () => {
      expect(seerr({ seerrApproves: true }).summary).toMatch(
        /approved there automatically, so they start downloading/,
      );
    });

    it("says a title is filed for approval when the account cannot approve", () => {
      expect(seerr({ seerrApproves: false }).summary).toMatch(
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
      const flow = seerr({ everythingAutoSends: true });
      expect(flow.summary).toMatch(/Nothing waits here/);
      expect(flow.doubleApproval).toBeUndefined();
    });
  });

  describe("approving the same title twice", () => {
    it("names it when auto-send is off and the account cannot approve", () => {
      const flow = seerr({ autoSend: false, seerrApproves: false });
      expect(flow.doubleApproval).toMatch(/twice/);
      expect(flow.doubleApproval).toMatch(
        /match the bars|approves automatically/,
      );
    });

    it("names it for the lower tier when only the strongest are sent", () => {
      const flow = seerr({ autoSend: true, seerrApproves: false });
      expect(flow.doubleApproval).toMatch(/approving again in Overseerr/);
    });

    it("stays quiet when there is genuinely only one gate", () => {
      // Every combination that does NOT make you approve twice.
      expect(seerr({ seerrApproves: true }).doubleApproval).toBeUndefined();
      expect(
        seerr({ autoSend: false, seerrApproves: true }).doubleApproval,
      ).toBeUndefined();
      expect(
        seerr({ everythingAutoSends: true, seerrApproves: false })
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
            seerrApproves: false,
          }).doubleApproval,
        ).toBeUndefined();
      }
    });
  });

  it("agrees in number with the subject it is completing", () => {
    // "Titles that clear the higher bars below GOES to Overseerr and IS approved" — one shared
    // phrase used after both a plural and a singular subject. Wrong in the commonest setup there is.
    const plural = seerr({ seerrApproves: true }).summary;
    expect(plural).toMatch(/Titles that clear the higher bars below go to/);
    expect(plural).not.toMatch(/below goes|and is approved|so it starts/);

    const singular = seerr({ autoSend: false, seerrApproves: true }).summary;
    expect(singular).toMatch(/it goes to Overseerr and is approved/);
    expect(singular).not.toMatch(/it go to|are approved/);
  });

  it("always produces a sentence, for every combination of inputs", () => {
    // The summary is load-bearing: an empty one would leave the section saying nothing at all.
    for (const viaSeerr of [true, false])
      for (const autoSend of [true, false])
        for (const everythingAutoSends of [true, false])
          for (const seerrApproves of [true, false, null]) {
            const flow = describeRequestFlow({
              viaSeerr,
              autoSend,
              everythingAutoSends,
              seerrApproves,
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
