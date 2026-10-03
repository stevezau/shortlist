import { ArrowUp } from "lucide-react";
import { useState } from "react";

import { SaveStatus } from "@/components/save-status";
import { Switch } from "@/components/ui/switch";
import { settingBool, settingString } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** The events the server sends, in its order (`services/notify.py` EVENTS), grouped for reading. */
const EVENT_GROUPS: {
  title: string;
  hint?: string;
  events: { id: string; label: string }[];
}[] = [
  {
    title: "Runs",
    events: [
      { id: "run.started", label: "A run started" },
      { id: "run.finished", label: "A run finished" },
      { id: "run.partial", label: "A run finished, but some people failed" },
      { id: "run.failed", label: "A run failed" },
      { id: "run.stopped", label: "A run stopped before it finished" },
    ],
  },
  {
    title: "Jobs",
    hint: "The privacy sync and playback credits run every few minutes, so they only speak up when something changed or went wrong.",
    events: [
      { id: "job.started", label: "A job started" },
      { id: "job.finished", label: "A job finished" },
      { id: "job.failed", label: "A job failed" },
    ],
  },
  {
    title: "Everything else",
    events: [
      { id: "privacy.exposure", label: "Someone can see a row that isn’t theirs" },
      { id: "requests.waiting", label: "Titles are waiting for your approval" },
      { id: "update.available", label: "A new version of Shortlist is out" },
    ],
  },
];

const ALL_EVENTS = EVENT_GROUPS.flatMap((group) => group.events.map((e) => e.id));

function storedEvents(settings: Settings): string[] {
  const value = settings["notify.webhook.events"];
  return Array.isArray(value)
    ? value.filter((v): v is string => typeof v === "string")
    : [];
}

/**
 * Tell the owner what happened while they were asleep, on the channel they already watch.
 *
 * Only the on/off switch and the choice of events live here. Where the messages go — the address and
 * an optional auth header — is the Webhook row just above it in Connections, with every other
 * service Shortlist talks to, where it gets Test and Remove like they do.
 *
 * The owner picks the events. The server's default is a failed run and a privacy exposure — the two
 * nobody can see before their next login — so a section nobody touches stays quiet. No message names a
 * person: the server keeps names in the app.
 */
export function NotificationsSection({ settings }: { settings: Settings }) {
  const save = useSaveSettings();
  const [enabled, setEnabled] = useState(
    settingBool(settings, "notify.webhook.enabled"),
  );
  const [events, setEvents] = useState(() => storedEvents(settings));
  const hasAddress = settingString(settings, "notify.webhook.url") !== "";

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

  // The same rule for each tick, and the whole list is sent: the setting is one list, not one key per event.
  const toggleEvent = (id: string) => {
    const previous = events;
    const chosen = new Set(previous);
    if (chosen.has(id)) chosen.delete(id);
    else chosen.add(id);
    const next = ALL_EVENTS.filter((e) => chosen.has(e));
    setEvents(next);
    save.mutate(
      { "notify.webhook.events": next },
      { onError: () => setEvents(previous) },
    );
  };

  return (
    <div className="space-y-4 px-4 py-4 sm:pl-16 sm:pr-5">
      <div className="flex items-start justify-between gap-4">
        <div className="space-y-0.5">
          <p className="text-sm font-medium">Send alerts to a webhook</p>
          <p className="text-sm text-muted-foreground">
            Posts a message for each thing you tick below. No message ever
            names anybody.
          </p>
        </div>
        <Switch
          checked={enabled}
          onCheckedChange={toggle}
          aria-label="Send alerts to a webhook"
        />
      </div>

      {/* The save readout shares the status line: on a line of its own its idle (empty, fixed-height)
          slot left a blank band in the middle of the block. A failure wraps onto its own line. */}
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2 [&>[role=alert]]:basis-full">
        <p className="text-sm text-muted-foreground">{hasAddress ? `${events.length} events selected · webhook configured` : "Webhook address missing"}</p>
        <SaveStatus fallback="Couldn’t save that. Try again." isPending={save.isPending} isError={save.isError} error={save.error} saved={save.isSuccess} onRetry={() => {
          const values = save.variables;
          if (!values) return;
          save.mutate(values, { onSuccess: () => {
            if (typeof values["notify.webhook.enabled"] === "boolean") setEnabled(values["notify.webhook.enabled"]);
            if (Array.isArray(values["notify.webhook.events"])) setEvents(values["notify.webhook.events"] as string[]);
          } });
        }} />
      </div>

      {enabled && !hasAddress && (
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
