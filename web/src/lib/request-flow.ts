/**
 * What will actually happen to a title tonight, in one plain sentence.
 *
 * On the Overseerr route a title can be stopped in two places — Shortlist's own inbox and Overseerr's
 * approval queue — and which ones apply is decided by three settings that are nowhere near each
 * other on the page: the auto-send toggle, the two auto-send bars against the guardrails, and the
 * permissions of the account requests are filed as. Working that out in your head is the actual UX
 * problem; nothing was WRONG, it just could not be read.
 *
 * So the screen answers it instead. This is a pure function of those inputs, which is what lets the
 * summary be tested exhaustively rather than eyeballed.
 */

export interface RequestFlowInput {
  /** Requests route through Overseerr rather than straight to Radarr/Sonarr. */
  viaSeerr: boolean;
  /** The strongest titles go out on their own, rather than every title waiting for approval. */
  autoSend: boolean;
  /** True when the auto-send bars are no higher than the guardrails, so everything qualifying goes. */
  everythingAutoSends: boolean;
  /**
   * Whether the chosen Overseerr account's requests skip Overseerr's approval queue. `null` when it
   * cannot be known — the account list has not loaded, or the instance could not be reached — in
   * which case the summary says what is certain and stays quiet about the rest.
   */
  seerrApproves: boolean | null;
}

export interface RequestFlow {
  /** The sentence. Always present, always true of the current settings. */
  summary: string;
  /**
   * Set only when the settings make you approve the SAME title twice — once here, once in Overseerr.
   * Not an error: it is a legitimate (if rarely wanted) choice, so it is named rather than blocked.
   */
  doubleApproval?: string;
}

const HERE = "waits here for your yes";

export function describeRequestFlow(input: RequestFlowInput): RequestFlow {
  const { viaSeerr, autoSend, everythingAutoSends, seerrApproves } = input;

  if (!viaSeerr) {
    // The Arr route has only ever had one gate, so there is nothing to disentangle.
    return {
      summary: autoSend
        ? `Titles that clear the higher bars below go to Radarr or Sonarr as soon as a run finds them. Everything else that clears your guardrails ${HERE}.`
        : `Every title that clears your guardrails ${HERE} — nothing is sent to Radarr or Sonarr without you.`,
    };
  }

  // Overseerr route. What "sent" means here depends entirely on the account it is filed as, which is
  // the fact the old copy quietly assumed away by saying "send" and meaning "added to your library".
  //
  // Two forms, because these phrases complete sentences with different subjects — "Titles that clear
  // the bars ..." against "once you approve it, it ...". One form for both produced
  // "Titles ... goes ... and is approved", which is the sort of thing that makes a screen feel
  // machine-written.
  const landsIn =
    seerrApproves === null
      ? "goes to Overseerr"
      : seerrApproves
        ? "goes to Overseerr and is approved there automatically, so it starts downloading"
        : "is filed in Overseerr for you to approve there";
  const landsInPlural =
    seerrApproves === null
      ? "go to Overseerr"
      : seerrApproves
        ? "go to Overseerr and are approved there automatically, so they start downloading"
        : "are filed in Overseerr for you to approve there";

  if (!autoSend) {
    const summary = `Every title that clears your guardrails ${HERE}. Nothing reaches Overseerr until you approve it — and once you do, it ${landsIn}.`;
    return seerrApproves === false
      ? {
          summary,
          doubleApproval:
            "You will approve each title twice: once here, then again in Overseerr. To decide in only one place, either turn this on and match the bars below to your guardrails, or pick an account above that approves automatically.",
        }
      : { summary };
  }

  if (everythingAutoSends) {
    // The setup that is possible today but undiscoverable: bars at the guardrails means Shortlist
    // stops deciding altogether and Overseerr becomes the only gate.
    return {
      summary: `Your bars match your guardrails, so every qualifying title ${landsIn}. Nothing waits here.`,
    };
  }

  const summary = `Titles that clear the higher bars below ${landsInPlural} as soon as a run finds them. Everything else that clears your guardrails ${HERE}.`;
  return seerrApproves === false
    ? {
        summary,
        doubleApproval:
          "The titles that wait here will need approving again in Overseerr afterwards. To decide only once, match the bars below to your guardrails — then everything goes straight to Overseerr and you approve it all there.",
      }
    : { summary };
}

/**
 * Do the auto-send bars let everything through?
 *
 * "No higher than", not "equal to": a bar BELOW the guardrail is just as inert as one that matches
 * it, and an owner who dragged it to zero to mean "send everything" would otherwise be told their
 * titles wait somewhere they do not.
 */
export function autoSendBarsMatchGuardrails(bars: {
  autoMinDemand: number;
  autoMinRating: number;
  minDemand: number;
  minRating: number;
}): boolean {
  return (
    bars.autoMinDemand <= bars.minDemand && bars.autoMinRating <= bars.minRating
  );
}
