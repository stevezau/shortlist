import { ArrowRight } from "lucide-react";
import { Link } from "react-router";

/**
 * The one link out of "the owner sees everyone's rows" and into something you can DO about it.
 *
 * Three places already explain the limitation correctly — the row editor's placement grid, the
 * Users page's owner note, and the setup wizard. All three ended at "watch on a second account",
 * which is advice with no next step, and the question kept coming back. This is that next step,
 * and it is deliberately the SAME component in all three so they can never drift apart or offer
 * different wording for the same escape hatch.
 */
export function WatchingAccountLink({ className }: { className?: string }) {
  return (
    <Link
      to="/watching-account"
      className={`inline-flex items-center gap-1 font-medium text-foreground underline underline-offset-4 hover:no-underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 ${className ?? ""}`}
    >
      See your options
      <ArrowRight className="h-3 w-3" aria-hidden="true" />
    </Link>
  );
}
