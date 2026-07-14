import { useState } from "react";

import {
  CurationStyleFields,
  type CurationStyleValue,
} from "@/components/curation-style";
import { SavedIndicator } from "@/components/saved-indicator";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { apiErrorMessage } from "@/lib/api";
import { settingString } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** The global curation recipe — tone, guidance, and an optional hand-written prompt. */
export function CurationSection({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const [curation, setCuration] = useState<CurationStyleValue>({
    tone: settingString(settings, "curator.prompt_tone", "balanced"),
    guidance: settingString(settings, "curator.prompt_guidance"),
    template: settingString(settings, "curator.prompt_template"),
  });
  const [justSaved, setJustSaved] = useState(false);

  const save = () => {
    setJustSaved(false);
    saveSettings.mutate(
      {
        "curator.prompt_tone": curation.tone,
        "curator.prompt_guidance": curation.guidance,
        "curator.prompt_template": curation.template,
      },
      { onSuccess: () => setJustSaved(true) },
    );
  };

  return (
    <section aria-labelledby="curation-heading" className="space-y-3">
      <h2 id="curation-heading" className="text-lg font-semibold">
        Curation style
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <p className="text-sm text-muted-foreground">
            How the AI writes everyone&rsquo;s rows. Set a tone and a few plain
            notes, or write the whole prompt yourself. You can override this per
            person on their page.
          </p>
          <CurationStyleFields value={curation} onChange={setCuration} />
          <div className="flex items-center gap-3">
            <Button onClick={save} loading={saveSettings.isPending}>
              Save curation style
            </Button>
            <SavedIndicator show={justSaved && !saveSettings.isPending} />
            {saveSettings.isError && (
              <p role="alert" className="text-sm text-destructive">
                {apiErrorMessage(
                  saveSettings.error,
                  "Saving failed. Try again.",
                )}
              </p>
            )}
          </div>
        </CardContent>
      </Card>
    </section>
  );
}
