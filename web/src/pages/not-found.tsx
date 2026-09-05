import { Link } from "react-router";

import { EmptyState } from "@/components/query-boundary";
import { Button } from "@/components/ui/button";

/**
 * The catch-all route.
 *
 * The hint used to end "Use the navigation on the left", which is false on a phone — `AppShell`
 * renders the nav as a slide-in drawer behind a hamburger, so the only instruction on the page
 * named something the reader could not see. A real way out needs no claim about the layout.
 */
export function NotFoundPage() {
  return (
    <EmptyState
      title="Page not found"
      hint="That page doesn't exist. It may have moved, or the link was wrong."
      action={
        <Button asChild variant="outline" size="sm">
          <Link to="/">Go to Dashboard</Link>
        </Button>
      }
    />
  );
}
