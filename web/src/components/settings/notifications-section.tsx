import { ArrowUp } from "lucide-react";

import { SaveStatus } from "@/components/save-status";
import { Switch } from "@/components/ui/switch";
import { EVENT_GROUPS, useWebhookAlerts, type WebhookAlerts } from "@/components/settings/webhook-alerts";
import type { Settings } from "@/lib/types";

/** The alerts switch, for the Webhook card's header: the switch sits with the webhook it sends to. */
export function WebhookAlertsSwitch({ alerts }: { alerts: WebhookAlerts }) {
  return (
    <label className="flex items-center gap-2 text-sm text-muted-foreground">
      Send alerts
      <Switch
        checked={alerts.enabled && alerts.hasAddress}
        disabled={!alerts.hasAddress}
        onCheckedChange={alerts.toggle}
        aria-label="Send alerts to a webhook"
      />
    </label>
  );
}

/**
 * The events to send, and the readout under the switch. Alone on a page it carries the switch too;
 * inside Connections the Webhook card owns the switch and passes `alerts` in.
 */
export function NotificationsSection({ settings, alerts }: { settings: Settings; alerts?: WebhookAlerts }) {
  const own = useWebhookAlerts(settings);
  const { save, enabled, toggle, events, toggleEvent, hasAddress, retry } = alerts ?? own;

  return (
    <div className="space-y-4 px-4 py-4 sm:pl-16 sm:pr-5">
      {!alerts && (
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-0.5">
            <p className="text-sm font-medium">Send alerts to a webhook</p>
            <p className="text-sm text-muted-foreground">
              Posts a message for each thing you tick below. No message ever
              names anybody.
            </p>
          </div>
          <Switch
            checked={enabled && hasAddress}
            disabled={!hasAddress}
            onCheckedChange={toggle}
            aria-label="Send alerts to a webhook"
          />
        </div>
      )}

      {/* The save readout shares the status line: on a line of its own its idle (empty, fixed-height)
          slot left a blank band in the middle of the block. A failure wraps onto its own line. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 [&>[role=alert]]:basis-full">
        <p className="text-sm text-muted-foreground">{hasAddress ? `${events.length} events selected · webhook configured` : "Webhook address missing"}</p>
        {(save.isPending || save.isError || save.isSuccess) && (
          <SaveStatus fallback="Couldn’t save that. Try again." isPending={save.isPending} isError={save.isError} error={save.error} saved={save.isSuccess} onRetry={retry} />
        )}
      </div>

      {!hasAddress && (
        <p className="flex flex-wrap items-center gap-x-2 gap-y-1 rounded-md border border-warning/40 bg-warning/5 px-3 py-2 text-sm">
          Nothing is sent until it has somewhere to go.
          <a
            href="#connection-notify"
            className="inline-flex items-center gap-1 font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            Set up the webhook above
            <ArrowUp className="h-3.5 w-3.5" aria-hidden="true" />
          </a>
        </p>
      )}

      {enabled ? (
        // Choosing what to send works before there is an address; only the switch waits for one.
        <fieldset className="space-y-3">
          <legend className="text-sm font-medium">What to send</legend>
          <div className="grid gap-x-8 gap-y-5 sm:grid-cols-2 lg:grid-cols-3">
            {EVENT_GROUPS.map((group) => (
              <div key={group.title} className="space-y-2">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  {group.title}
                </p>
                <div className="space-y-1">
                  {group.events.map((event) => (
                    <label
                      key={event.id}
                      className="flex cursor-pointer items-start gap-2.5 rounded-md px-2 py-1.5 text-sm transition-colors hover:bg-muted/50"
                    >
                      <input
                        type="checkbox"
                        checked={events.includes(event.id)}
                        onChange={() => toggleEvent(event.id)}
                        className="mt-0.5 h-4 w-4 shrink-0 accent-primary"
                      />
                      {event.label}
                    </label>
                  ))}
                </div>
                {/* Under the list, so the three columns' checkboxes start on the same line. */}
                {group.hint && (
                  <p className="px-2 text-xs text-muted-foreground">{group.hint}</p>
                )}
              </div>
            ))}
          </div>
        </fieldset>
      ) : (
        <p className="text-sm text-muted-foreground">
          Until this is on, a failed run shows up in the bell at the top of
          the page — the next time you look.
        </p>
      )}
    </div>
  );
}
