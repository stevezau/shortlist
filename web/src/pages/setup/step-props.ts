import type { WizardApi } from "@/lib/wizard";

/** What a step can put in the shell's page header in place of its static title (the finished run). */
export interface StepHeader {
  title: string;
  why: string;
  /** Replaces "Step N of M" in the corner, e.g. "Step 7 of 7 · Done". */
  stepLabel?: string;
  /** A status pill beside the title. */
  badge?: { text: string; variant: "success" | "warning" | "destructive" };
}

/** Contract between the wizard shell and each step component. */
export type StepProps = Pick<
  WizardApi,
  "data" | "update" | "next" | "complete"
> & {
  back?: WizardApi["back"];
  /** Overrides the shell's header for this step; pass null to restore the static one. */
  setHeader?: (header: StepHeader | null) => void;
};
