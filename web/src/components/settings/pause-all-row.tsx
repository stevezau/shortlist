import { MutationAlert } from "@/components/mutation-alert";
import { SettingRow } from "@/components/settings/section-layout";
import { Button } from "@/components/ui/button";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** Pause every user at once. Reversible, so it sits with Run speed rather than under the Danger zone.
 *  The Users page has no bulk pause, so this is the only place it is. */
export function PauseAllRow({ settings }: { settings: Settings }) {
  const saveSettings = useSaveSettings();
  const pausedAll = settings["paused_all"] === true;

  return (
    <SettingRow
      id="pause-all"
      title={pausedAll ? "Everything is paused" : "Pause all users"}
      // Precise: a run still STARTS (`enabled_profiles` returns [] while paused, and the engine still
      // does its privacy sweep on an empty user list). What stops is any row being built or re-picked.
      description="Nobody is processed on any run, scheduled or manual, until you resume, so no row is rebuilt and nobody’s picks change. Nobody is enabled or disabled, and the rows already on Plex stay where they are."
      control={
        <Button
          variant="outline"
          onClick={() => saveSettings.mutate({ paused_all: !pausedAll })}
          loading={saveSettings.isPending}
        >
          {pausedAll ? "Resume all" : "Pause all"}
        </Button>
      }
    >
      {saveSettings.isError && (
        <MutationAlert
          error={saveSettings.error}
          fallback="Couldn’t change whether processing is paused. Try again."
          onRetry={() => saveSettings.mutate({ paused_all: !pausedAll })}
        />
      )}
      {saveSettings.isSuccess && (
        <p role="status" className="text-sm text-success">
          {saveSettings.variables?.paused_all
            ? "Processing paused. Existing rows stay where they are."
            : "Processing resumed."}
        </p>
      )}
    </SettingRow>
  );
}
