import { Gauge } from "lucide-react";

import { ImpactReport } from "@/components/dashboard/impact-report";
import { PageHeader } from "@/components/page-header";

/**
 * The dashboard is the tracking report: what Shortlist delivered and what people actually watched.
 * The per-user list lives on the Users page (no need to duplicate it here).
 */
export function DashboardPage() {
  return (
    <div>
      <PageHeader
        icon={Gauge}
        title="Dashboard"
        subtitle="Is any of this working? A title is “delivered” once Shortlist puts it in someone's row — this page counts those, and how many were actually watched."
      />
      <ImpactReport />
    </div>
  );
}
