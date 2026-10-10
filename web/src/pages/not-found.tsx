import { Link, useLocation } from "react-router";

import { PageHeader } from "@/components/page-header";
import { Button } from "@/components/ui/button";

/**
 * The catch-all route.
 *
 * The hint makes no claim about the layout ("Use the navigation on the left" is false on a phone,
 * where `AppShell` renders the nav as a slide-in drawer behind a hamburger).
 */
export function NotFoundPage() {
  const { pathname, search } = useLocation();
  return (
    <div className="space-y-4">
      <PageHeader
        className="mb-0"
        title="Page not found"
        subtitle="That page doesn't exist. It may have moved, or the link was wrong."
      />
      <p className="text-sm text-muted-foreground">
        Address tried:{" "}
        <code className="break-all rounded bg-muted px-1 py-0.5 font-mono text-xs">{`${pathname}${search}`}</code>
      </p>
      <Button asChild>
        <Link to="/">Go to Dashboard</Link>
      </Button>
    </div>
  );
}
