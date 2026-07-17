import { useState } from "react";

import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { SaveStatus } from "@/components/save-status";
import { Card, CardContent } from "@/components/ui/card";
import { useAutosavedSettings } from "@/lib/autosave";
import { settingString } from "@/lib/format";
import type { Settings } from "@/lib/types";

/** The global curation recipe — tone, guidance, and an optional hand-written prompt. */
export function CurationSection({ settings }: { settings: Settings }) {
  const [curation, setCuration] = useState<CurationStyleValue>({
    tone: settingString(settings, "curator.prompt_tone", "balanced"),
    guidance: settingString(settings, "curator.prompt_guidance"),
    template: settingString(settings, "curator.prompt_template"),
  });

  const save = useAutosavedSettings(curation, () => ({
    "curator.prompt_tone": curation.tone,
    "curator.prompt_guidance": curation.guidance,
    "curator.prompt_template": curation.template,
  }));

  return (
    <section aria-labelledby="curation-heading" className="space-y-3">
      <h2 id="curation-heading" className="text-lg font-semibold">
        Curation style
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <p className="text-sm text-muted-foreground">
            How the AI writes everyone&rsquo;s rows — edit the prompt directly.
          </p>
          <CurationStyleFields
            value={curation}
            onChange={setCuration}
            scope="global"
          />
          <SaveStatus
            isPending={save.isPending}
            isError={save.isError}
            error={save.error}
            saved={save.saved}
            onRetry={save.retry}
          />
        </CardContent>
      </Card>
    </section>
  );
}
