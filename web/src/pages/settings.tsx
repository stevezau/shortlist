import { Settings as SettingsIcon } from "lucide-react";
import type { ReactNode } from "react";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary } from "@/components/query-boundary";
import { AdvancedSection } from "@/components/settings/advanced-section";
import { ApiAccessCard } from "@/components/settings/api-access-card";
import { ConnectionsSection } from "@/components/settings/connections-section";
import { CurationSection } from "@/components/settings/curation-section";
import { DangerZoneSection } from "@/components/settings/danger-zone-section";
import { DefaultsSection } from "@/components/settings/defaults-section";
import { RecommendationsSection } from "@/components/settings/recommendations-section";
import { RequestsSection } from "@/components/settings/requests-section";
import { RowPlacementSection } from "@/components/settings/row-placement-section";
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
    placement: <RowPlacementSection settings={settings} />,
    requests: <RequestsSection settings={settings} />,
    advanced: <AdvancedSection settings={settings} />,
    "api-access": <ApiAccessCard />,
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
        subtitle="Everything that shapes how Shortlist runs — connections, where picks come from, and how rows look. Each row keeps its own schedule."
      />

      <QueryBoundary
        query={settingsQuery}
        skeleton={<Skeleton className="h-96 w-full" />}
      >
        {(settings) => {
          const content = sectionContent(settings);
          return (
            <>
              {/* On phones the sidebar sub-nav is hidden, so jumping between sections meant scrolling
                  the whole page. This gives mobile its own horizontally-scrollable section jumper. */}
              <nav
                aria-label="Settings sections"
                className="mb-4 flex gap-1.5 overflow-x-auto pb-2 md:hidden"
              >
                {SETTINGS_SECTIONS.map(({ id, label, icon: Icon }) => (
                  <a
                    key={id}
                    href={`#${id}`}
                    className="flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                  >
                    <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    {label}
                  </a>
                ))}
              </nav>
              <div className="space-y-8">
                {SETTINGS_SECTIONS.map(({ id }) => (
                  // scroll-mt keeps the heading clear of the top when a sub-nav jumps here.
                  <section key={id} id={id} className="scroll-mt-6">
                    {content[id]}
                  </section>
                ))}
              </div>
            </>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
