import { Settings as SettingsIcon } from "lucide-react";
import type { ReactNode } from "react";
import { useLocation } from "react-router";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary } from "@/components/query-boundary";
import { AdvancedSection } from "@/components/settings/advanced-section";
import { ApiAccessCard } from "@/components/settings/api-access-card";
import { ConnectionsSection } from "@/components/settings/connections-section";
import { DangerZoneSection } from "@/components/settings/danger-zone-section";
import { DefaultsSection } from "@/components/settings/defaults-section";
import { RecommendationsSection } from "@/components/settings/recommendations-section";
import { RequestsSection } from "@/components/settings/requests-section";
import { RowPlacementSection } from "@/components/settings/row-placement-section";
import {
  activeSectionId,
  SETTINGS_SECTIONS,
} from "@/components/settings/sections";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** Each section's content, keyed by the id in SETTINGS_SECTIONS (the sidebar sub-nav lists them). */
function sectionContent(settings: Settings): Record<string, ReactNode> {
  return {
    connections: <ConnectionsSection settings={settings} />,
    recommendations: <RecommendationsSection settings={settings} />,
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
  // One section on screen at a time, chosen by `#hash` (the sidebar sub-nav and the mobile jumper
  // both link to `#id`). A single long stack put eight sections' worth of sub-headings in one
  // column, so nothing showed where a section's options ended — see `SettingsSubNav`.
  const active = activeSectionId(useLocation().hash);

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
              {/* The sidebar sub-nav is hidden on phones, so mobile gets its own horizontally
                  scrollable switcher for the same sections. */}
              <nav
                aria-label="Settings sections"
                className="mb-4 flex gap-1.5 overflow-x-auto pb-2 md:hidden"
              >
                {SETTINGS_SECTIONS.map(({ id, label, icon: Icon }) => (
                  <a
                    key={id}
                    href={`#${id}`}
                    aria-current={active === id ? "true" : undefined}
                    className={
                      active === id
                        ? "flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border border-primary bg-primary/10 px-3 py-1.5 text-sm font-medium text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                        : "flex shrink-0 items-center gap-1.5 whitespace-nowrap rounded-full border px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
                    }
                  >
                    <Icon className="h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                    {label}
                  </a>
                ))}
              </nav>
              <section id={active} className="scroll-mt-6">
                {content[active]}
              </section>
            </>
          );
        }}
      </QueryBoundary>
    </div>
  );
}
