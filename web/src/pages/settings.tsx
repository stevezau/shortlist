import { Settings as SettingsIcon } from "lucide-react";
import type { ReactNode } from "react";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary } from "@/components/query-boundary";
import { AdvancedSection } from "@/components/settings/advanced-section";
import { ConnectionsSection } from "@/components/settings/connections-section";
import { CurationSection } from "@/components/settings/curation-section";
import { DangerZoneSection } from "@/components/settings/danger-zone-section";
import { DefaultsSection } from "@/components/settings/defaults-section";
import { PrivacySection } from "@/components/settings/privacy-section";
import { RecommendationsSection } from "@/components/settings/recommendations-section";
import { RequestsSection } from "@/components/settings/requests-section";
import { ScheduleSection } from "@/components/settings/schedule-section";
import { SETTINGS_SECTIONS } from "@/components/settings/sections";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** Each section's content, keyed by the id in SETTINGS_SECTIONS (the sidebar sub-nav lists them). */
function sectionContent(settings: Settings): Record<string, ReactNode> {
  return {
    connections: <ConnectionsSection settings={settings} />,
    recommendations: <RecommendationsSection settings={settings} />,
    curation: <CurationSection settings={settings} />,
    defaults: <DefaultsSection settings={settings} />,
    schedule: <ScheduleSection settings={settings} />,
    requests: <RequestsSection settings={settings} />,
    privacy: <PrivacySection />,
    advanced: <AdvancedSection settings={settings} />,
    danger: <DangerZoneSection settings={settings} />,
  };
}

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
        {(settings) => {
          const content = sectionContent(settings);
          return (
            <div className="space-y-8">
              {SETTINGS_SECTIONS.map(({ id }) => (
                // scroll-mt keeps the heading clear of the top when the sidebar sub-nav jumps here.
                <section key={id} id={id} className="scroll-mt-6">
                  {content[id]}
                </section>
              ))}
            </div>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
