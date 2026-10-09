import { Link } from "react-router";

import { PageHeader } from "@/components/page-header";
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
    <PageHeader
      title="Page not found"
      subtitle="That page doesn't exist. It may have moved, or the link was wrong."
      actions={
        <Button asChild>
          <Link to="/">Go to Dashboard</Link>
        </Button>
      }
    />
  );
}
