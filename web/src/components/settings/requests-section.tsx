import { RequestsSettings } from "@/components/requests-settings";
import { SettingsSection } from "@/components/settings/section-layout";
import type { Settings } from "@/lib/types";

/** Requests: auto-fill missing picks via Radarr/Sonarr or Overseerr. The panel owns its own form,
 *  save and "Saved" readout, which stays beside the form rather than in the tab's save bar. */
export function RequestsSection({ settings }: { settings: Settings }) {
  return (
    <SettingsSection id="requests" title="Requests" description="Ask for great picks that aren’t in your library yet.">
      <RequestsSettings settings={settings} />
    </SettingsSection>
  );
}
