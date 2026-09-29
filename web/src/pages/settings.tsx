import type { ReactNode } from "react";

import { QueryBoundary } from "@/components/query-boundary";
import { AdvancedSection } from "@/components/settings/advanced-section";
import { ApiAccessCard } from "@/components/settings/api-access-card";
import { ConnectionsSection } from "@/components/settings/connections-section";
import { DangerZoneSection } from "@/components/settings/danger-zone-section";
import { DefaultsSection } from "@/components/settings/defaults-section";
import { NotificationsSection } from "@/components/settings/notifications-section";
import { RecommendationsSection } from "@/components/settings/recommendations-section";
import { RequestsSection } from "@/components/settings/requests-section";
import { RowPlacementSection } from "@/components/settings/row-placement-section";
import { SettingsSections } from "@/components/settings/section-layout";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** Each section's content, keyed by the id in SETTINGS_SECTIONS (the in-page navigation lists them). */
function sectionContent(settings: Settings): Record<string, ReactNode> {
  return {
    connections: <ConnectionsSection settings={settings} />,
    recommendations: <RecommendationsSection settings={settings} />,
    defaults: <DefaultsSection settings={settings} />,
    placement: <RowPlacementSection settings={settings} />,
    requests: <RequestsSection settings={settings} />,
    notifications: <NotificationsSection settings={settings} />,
    advanced: <AdvancedSection settings={settings} />,
    "api-access": <ApiAccessCard />,
    danger: <DangerZoneSection settings={settings} />,
  };
}

export function SettingsPage() {
  const settingsQuery = useSettings();

  return (
    <div id="settings-page-top" className="mx-auto max-w-6xl scroll-mt-24">
      <header className="space-y-2">
        <h1 className="text-3xl font-semibold tracking-tight">Settings</h1>
        <p className="text-sm text-muted-foreground">Make Shortlist work your way.</p>
      </header>

      <QueryBoundary
        query={settingsQuery}
        skeleton={<Skeleton className="h-96 w-full" />}
      >
        {(settings) => {
          return <SettingsSections content={sectionContent(settings)} />;
        }}
      </QueryBoundary>
    </div>
  );
}
