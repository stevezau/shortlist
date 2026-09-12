import { Gauge } from "lucide-react";

import { ImpactReport } from "@/components/dashboard/impact-report";
import { PageHeader } from "@/components/page-header";

/**
 * The dashboard is the tracking report: what Shortlist delivered and what people actually watched.
 * The per-user list lives on the Users page (no need to duplicate it here).
 *
 * "Is anything wrong" is the notification bell's job. A strip of per-area chips used to sit here
 * too, but every chip was derived from the same alerts the bell already lists.
 */
export function DashboardPage() {
  return (
    <div>
      <PageHeader
        icon={Gauge}
        title="Dashboard"
        subtitle="What Shortlist put in people's rows, and how much of it they watched."
      />
      <ImpactReport />
    </div>
  );
}
