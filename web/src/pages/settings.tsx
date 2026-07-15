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
import { SettingsNav } from "@/components/settings/settings-nav";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

// Ordered as a new owner works down the page: connect things → decide where titles come from → how
// they're written → row/schedule defaults → optional requests → privacy → advanced → danger. The
// `id`s anchor the side-rail nav; the labels are what it lists.
function sections(
  settings: Settings,
): { id: string; label: string; el: ReactNode }[] {
  return [
    {
      id: "connections",
      label: "Connections",
      el: <ConnectionsSection settings={settings} />,
    },
    {
      id: "recommendations",
      label: "Recommendations",
      el: <RecommendationsSection settings={settings} />,
    },
    {
      id: "curation",
      label: "Curation",
      el: <CurationSection settings={settings} />,
    },
    {
      id: "defaults",
      label: "Row defaults",
      el: <DefaultsSection settings={settings} />,
    },
    {
      id: "schedule",
      label: "Schedule",
      el: <ScheduleSection settings={settings} />,
    },
    {
      id: "requests",
      label: "Requests",
      el: <RequestsSection settings={settings} />,
    },
    { id: "privacy", label: "Privacy", el: <PrivacySection /> },
    {
      id: "advanced",
      label: "Advanced",
      el: <AdvancedSection settings={settings} />,
    },
    {
      id: "danger",
      label: "Danger zone",
      el: <DangerZoneSection settings={settings} />,
    },
  ];
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
          const items = sections(settings);
          return (
            <div className="lg:grid lg:grid-cols-[11rem_minmax(0,1fr)] lg:gap-10">
              <SettingsNav
                sections={items.map(({ id, label }) => ({ id, label }))}
              />
              <div className="space-y-8">
                {items.map(({ id, el }) => (
                  // scroll-mt keeps the section heading clear of the top when the nav jumps to it.
                  <div key={id} id={id} className="scroll-mt-6">
                    {el}
                  </div>
                ))}
              </div>
            </div>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
