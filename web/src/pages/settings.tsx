import { Bot } from "lucide-react";
import { Link } from "react-router";

import { PageHeader } from "@/components/page-header";
import { QueryBoundary } from "@/components/query-boundary";
import { AdvancedSection } from "@/components/settings/advanced-section";
import { ApiAccessCard } from "@/components/settings/api-access-card";
import { AssistantAccessCard } from "@/components/settings/assistant-access-card";
import { ConnectionsSection } from "@/components/settings/connections-section";
import { DangerZoneSection } from "@/components/settings/danger-zone-section";
import { DefaultsSection } from "@/components/settings/defaults-section";
import { RecommendationsSection } from "@/components/settings/recommendations-section";
import { RequestsSection } from "@/components/settings/requests-section";
import { RowPlacementSection } from "@/components/settings/row-placement-section";
import { SaveBar, SaveBarProvider } from "@/components/settings/save-bar";
import { SectionsWithJumps, SettingsTabs } from "@/components/settings/section-layout";
import { DEFAULTS_SECTIONS } from "@/components/settings/sections";
import { SettingsSearch } from "@/components/settings/settings-search";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** Connections: every service Shortlist talks to, and where its alerts go. */
function ConnectionsTab({ settings }: { settings: Settings }) {
  return <ConnectionsSection settings={settings} />;
}

/** Defaults: what every new row starts from. Each section still saves itself as before; the bar at
 *  the foot reports all of them at once. */
function DefaultsTab({ settings }: { settings: Settings }) {
  return (
    <SaveBarProvider>
      <SectionsWithJumps tab="defaults" label="Defaults sections" sections={DEFAULTS_SECTIONS}>
        <RecommendationsSection settings={settings} />
        <DefaultsSection settings={settings} />
        <RowPlacementSection settings={settings} />
      </SectionsWithJumps>
      <SaveBar />
    </SaveBarProvider>
  );
}

/** Requests: filling gaps in a library through Radarr, Sonarr or Overseerr. It had been the last
 *  section of Defaults, about 1,700px of that tab on its own. */
function RequestsTab({ settings }: { settings: Settings }) {
  return <RequestsSection settings={settings} />;
}

/** System: how Shortlist runs, the API token, and the danger zone. */
function SystemTab({ settings }: { settings: Settings }) {
  return (
    <SaveBarProvider>
      <div className="space-y-10">
        <AdvancedSection settings={settings} />
        <ApiAccessCard />
        <AssistantAccessCard />
        <DangerZoneSection />
      </div>
      <SaveBar />
    </SaveBarProvider>
  );
}

/**
 * Settings: four tabs at `/settings/connections`, `/settings/defaults`, `/settings/requests` and
 * `/settings/system`.
 * `/settings` and the old single-page `/settings#section` links land on the tab that section lives
 * on now (see `SettingsTabs`).
 */
export function SettingsPage() {
  const settingsQuery = useSettings();

  return (
    <div className="mx-auto max-w-6xl">
      <PageHeader
        title="Settings"
        subtitle="Your services, the defaults every new row starts from, and how Shortlist runs."
        actions={<>
          <Button asChild variant="outline"><Link to="/assistant-access"><Bot aria-hidden="true" /> AI assistants</Link></Button>
          <SettingsSearch />
        </>}
      />

      <QueryBoundary query={settingsQuery} skeleton={<Skeleton className="h-96 w-full" />}>
        {(settings) => (
          <SettingsTabs
            content={{
              connections: <ConnectionsTab settings={settings} />,
              defaults: <DefaultsTab settings={settings} />,
              requests: <RequestsTab settings={settings} />,
              system: <SystemTab settings={settings} />,
            }}
          />
        )}
      </QueryBoundary>
    </div>
  );
}
