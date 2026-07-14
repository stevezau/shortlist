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
        subtitle="Connections, schedule, row defaults, requests, privacy, and uninstall."
      />

      <QueryBoundary
        query={settingsQuery}
        skeleton={<Skeleton className="h-96 w-full" />}
      >
        {(settings) => (
          <div className="space-y-8">
            <ConnectionsSection settings={settings} />
            <ScheduleSection settings={settings} />
            <DefaultsSection settings={settings} />
            <CurationSection settings={settings} />
            <RecommendationsSection settings={settings} />
            <RequestsSection settings={settings} />
            <PrivacySection />
            <DangerZoneSection settings={settings} />
          </div>
        )}
      </QueryBoundary>
    </div>
  );
}
