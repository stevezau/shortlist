import { Settings as SettingsIcon } from "lucide-react";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary } from "@/components/query-boundary";
import { ConnectionsSection } from "@/components/settings/connections-section";
import { CurationSection } from "@/components/settings/curation-section";
import { DangerZoneSection } from "@/components/settings/danger-zone-section";
import { DefaultsSection } from "@/components/settings/defaults-section";
import { PrivacySection } from "@/components/settings/privacy-section";
import { RecommendationsSection } from "@/components/settings/recommendations-section";
import { RequestsSection } from "@/components/settings/requests-section";
import { ScheduleSection } from "@/components/settings/schedule-section";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/lib/queries";

export function SettingsPage() {
  const settingsQuery = useSettings();

  return (
    <div>
      <PageHeader
        icon={SettingsIcon}
        title="Settings"
        subtitle="Connections, recommendations, curation style, row defaults, schedule, requests, privacy, and uninstall."
      />

      <QueryBoundary
        query={settingsQuery}
        skeleton={<Skeleton className="h-96 w-full" />}
      >
        {(settings) => (
          // Ordered as a new owner works down the page: connect things → decide where titles come
          // from → how they're written → row/schedule defaults → optional requests → privacy/danger.
          <div className="space-y-8">
            <ConnectionsSection settings={settings} />
            <RecommendationsSection settings={settings} />
            <CurationSection settings={settings} />
            <DefaultsSection settings={settings} />
            <ScheduleSection settings={settings} />
            <RequestsSection settings={settings} />
            <PrivacySection />
            <DangerZoneSection settings={settings} />
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
