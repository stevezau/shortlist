import { badgeVariants } from "@/components/ui/badge";
import type { SeasonVerdict } from "@/lib/season-verdict";
import { cn } from "@/lib/utils";

/** A season's verdict for this row (#137 D10), as a chip: amber when the row won't fill or will look
 *  alike, green when there are enough. The words carry the meaning; the colour only repeats it. */
export function SeasonVerdictChip({ verdict }: { verdict: SeasonVerdict }) {
  return (
    <span
      className={cn(
        badgeVariants({ variant: verdict.level === "ok" ? "success" : "warning" }),
        "whitespace-normal text-left font-medium leading-snug",
      )}
    >
      {verdict.text}
    </span>
  );
}
