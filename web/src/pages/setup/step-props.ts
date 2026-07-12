import type { WizardApi } from "@/lib/wizard";

/** Contract between the wizard shell and each step component. */
export type StepProps = Pick<
  WizardApi,
  "data" | "update" | "next" | "complete"
>;
