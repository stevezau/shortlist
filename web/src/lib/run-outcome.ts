import type { RunFinishedEvent } from "@/lib/types";

/** What a finished run's status MEANS, in one place.
 *
 * A run ends three ways, and two of them are not failures. Both the header pill and the wizard's
 * first-run panel used to compute `status !== "ok"`, which told an owner who had just pressed Stop
 * that their run had *failed* — and, in the wizard, that "no rows were built". Neither is true:
 * cancellation is cooperative, so the engine stops taking NEW users but still runs the privacy merge
 * and the promote for everyone already delivered. Their rows are live.
 *
 * Kept as a function over the raw status rather than a boolean per caller, because a boolean is
 * exactly what collapsed three outcomes into two the first time.
 */
export type RunOutcome = "ok" | "stopped" | "failed";

export function runOutcome(status: RunFinishedEvent["status"]): RunOutcome {
  if (status === "ok") return "ok";
  // `aborted` is the engine's word for "the owner cancelled it", set when the cooperative cancel
  // flag was seen. Anything else that is not "ok" is a genuine failure.
  return status === "aborted" ? "stopped" : "failed";
}
