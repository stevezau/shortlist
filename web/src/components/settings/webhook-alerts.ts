import { useState } from "react";

import { settingBool, settingString } from "@/lib/format";
import { useSaveSettings } from "@/lib/queries";
import type { Settings } from "@/lib/types";

/** The events the server sends, in its order (`services/notify.py` EVENTS), grouped for reading. */
export const EVENT_GROUPS: {
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
      { id: "job.skipped", label: "A scheduled job was skipped" },
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
 * Tell the owner what happened while they were asleep, on the channel they already watch: the
 * on/off switch and the chosen events, each saved the moment it changes.
 *
 * Where the messages go — the address and an optional auth header — is the Webhook card in
 * Connections, with every other service Shortlist talks to, where it gets Test and Remove like they do.
 *
 * The owner picks the events. The server's default is a failed run and a privacy exposure — the two
 * nobody can see before their next login — so a section nobody touches stays quiet. No message names a
 * person: the server keeps names in the app.
 */
export function useWebhookAlerts(settings: Settings) {
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

  const retry = () => {
    const values = save.variables;
    if (!values) return;
    save.mutate(values, {
      onSuccess: () => {
        if (typeof values["notify.webhook.enabled"] === "boolean") setEnabled(values["notify.webhook.enabled"]);
        if (Array.isArray(values["notify.webhook.events"])) setEvents(values["notify.webhook.events"] as string[]);
      },
    });
  };

  return { save, enabled, toggle, events, toggleEvent, hasAddress, retry };
}

export type WebhookAlerts = ReturnType<typeof useWebhookAlerts>;
