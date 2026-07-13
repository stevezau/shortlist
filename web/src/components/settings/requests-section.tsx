import { RequestsSettings } from "@/components/requests-settings";
import type { Settings } from "@/lib/types";

/** Requests: auto-fill missing picks via Radarr/Sonarr. The panel owns its own form + save. */
export function RequestsSection({ settings }: { settings: Settings }) {
  return (
    <section aria-labelledby="requests-heading" className="space-y-3">
      <h2 id="requests-heading" className="text-lg font-semibold">
        Requests
      </h2>
      <RequestsSettings settings={settings} />
    </section>
  );
}
