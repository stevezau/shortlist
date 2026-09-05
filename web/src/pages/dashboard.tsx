import { Gauge } from "lucide-react";

import { HealthStrip } from "@/components/dashboard/health-strip";
import { ImpactReport } from "@/components/dashboard/impact-report";
import { PageHeader } from "@/components/page-header";

/**
 * The dashboard is the tracking report: what Shortlist delivered and what people actually watched.
 * The per-user list lives on the Users page (no need to duplicate it here).
 *
 * The strip sits above the report because it answers a different question — "is anything wrong" —
 * and the report answers it for nothing but the last run.
 */
export function DashboardPage() {
  return (
    <div>
      <PageHeader
        icon={Gauge}
        title="Dashboard"
        subtitle="What Shortlist put in people's rows, and how much of it they watched."
      />
      <HealthStrip />
      <ImpactReport />
    </div>
  );
}
