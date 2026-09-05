import { Card, CardContent } from "@/components/ui/card";
import { Switch } from "@/components/ui/switch";
import { useSaveSettings } from "@/lib/queries";
import { settingBool } from "@/lib/format";
import type { Settings } from "@/lib/types";
import { useState } from "react";

import { InlineKeyField } from "./inline-key-field";

/**
 * Tell me when a run fails — the one thing Shortlist could not say without the owner opening the app.
 *
 * Deliberately narrow, and the copy says so out loud. Only a whole run failing goes out, because that
 * is the only alert an owner cannot see at their leisure; a notifier that also pinged about updates
 * and pending requests is one they would mute inside a week, and then the 3am failure would be muted
 * too. Everything else stays in the bell.
 *
 * The address field is `InlineKeyField` rather than a bespoke form: it already handles the redacted
 * sentinel (a saved value shows as dots, and saving without retyping is a no-op rather than a wipe),
 * which this needs for exactly the same reason the API keys do — the URL IS the credential.
 */
export function NotificationsSection({ settings }: { settings: Settings }) {
  const save = useSaveSettings();
  const [enabled, setEnabled] = useState(
    settingBool(settings, "notify.webhook.enabled"),
  );

  // Flip immediately so the switch feels like a switch, but put it BACK if the save fails. A switch
  // left showing "on" over a server that still says off is the one outcome worse than a slow switch:
  // the owner walks away believing they will be told when a run fails, and they won't be.
  const toggle = (next: boolean) => {
    setEnabled(next);
    save.mutate(
      { "notify.webhook.enabled": next },
      { onError: () => setEnabled(!next) },
    );
  };

  return (
    <div className="space-y-4">
      <div>
        <h2 className="text-lg font-semibold">Notifications</h2>
        <p className="text-sm text-muted-foreground">
          Shortlist runs while you’re asleep. This is how it tells you when a
          run didn’t work.
        </p>
      </div>

      <Card>
        <CardContent className="space-y-4 pt-6">
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-0.5">
              <p className="text-sm font-medium">Send failures to a webhook</p>
              <p className="text-sm text-muted-foreground">
                Posts a message to Discord, Slack, Home Assistant, n8n —
                anything that accepts a webhook — when a whole run fails.
                Nothing else is sent: no updates, no requests, no per-person
                detail, and never anybody’s name.
              </p>
            </div>
            <Switch
              checked={enabled}
              onCheckedChange={toggle}
              aria-label="Send failures to a webhook"
            />
          </div>

          {save.isError && (
            <p role="alert" className="text-sm text-destructive-text">
              Couldn’t save that. Try the switch again.
            </p>
          )}

          {enabled ? (
            <InlineKeyField
              settingKey="notify.webhook.url"
              label="Webhook address"
              service="notify"
              settings={settings}
              placeholder="https://discord.com/api/webhooks/…"
              hint="Paste the address your chat app gave you. Anyone holding it can post to that channel, so it’s stored encrypted and shown as dots once saved."
              testLabel="Send a test"
            />
          ) : (
            <p className="text-sm text-muted-foreground">
              Turn this on to add a webhook address. Until then, a failed run
              shows up in the bell at the top of the page — the next time you
              look.
            </p>
          )}
        </CardContent>
      </Card>
    </div>
  );
}
