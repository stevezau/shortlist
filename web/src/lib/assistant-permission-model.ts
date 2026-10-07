import type { AssistantStatus } from "@/lib/types";

export type AssistantMode = "suggest" | "manage" | "custom";
export type SelectedDestination = { service_id: string; destination_id: string };

const OPTIONAL = ["history.export", "history.providers", "ai.generate", "requests.send"];

export function classifyAccess(capabilities: string[], status: AssistantStatus): AssistantMode {
  const withoutOptions = capabilities.filter((capability) => !OPTIONAL.includes(capability));
  const same = (baseline: string[]) => withoutOptions.length === baseline.length &&
    withoutOptions.every((capability) => baseline.includes(capability));
  if (same(status.presets.owner_automation)) return "manage";
  if (same(status.presets.inspect) && !capabilities.includes("requests.send")) return "suggest";
  return "custom";
}

export function baselineCapabilities(status: AssistantStatus, mode: "suggest" | "manage"): string[] {
  return [...status.presets[mode === "manage" ? "owner_automation" : "inspect"]];
}

export function switchAccessMode(
  capabilities: string[], status: AssistantStatus, mode: "suggest" | "manage",
): string[] {
  const optional = capabilities.filter((capability) => OPTIONAL.includes(capability) &&
    (mode === "manage" || capability !== "requests.send"));
  return [...new Set([...baselineCapabilities(status, mode), ...optional])];
}

export function selectedDestinationSnapshots(
  destinations: string[], original: string[], selections: SelectedDestination[],
): SelectedDestination[] {
  return selections.filter((selection) => destinations.includes(selection.destination_id) &&
    !original.includes(selection.destination_id));
}
