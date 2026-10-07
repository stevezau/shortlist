import { describe, expect, it } from "vitest";

import { baselineCapabilities, classifyAccess, selectedDestinationSnapshots, switchAccessMode } from "@/lib/assistant-permission-model";
import type { AssistantStatus } from "@/lib/types";

const status: AssistantStatus = {
  enabled: true,
  resource: "https://shortlist.example/mcp",
  issuer: "https://shortlist.example/assistant/oauth",
  configuration_error: null,
  configuration_hint: "",
  presets: {
    inspect: ["instance.read", "changes.prepare"],
    manage_selected_rows: ["instance.read", "changes.prepare", "rows.update"],
    owner_automation: ["instance.read", "changes.prepare", "rows.update", "config.write"],
  },
  setting_groups: ["schedule", "row_defaults"],
};

describe("assistant access mapping", () => {
  it("classifies actual rights, so an inspect-labelled grant with writes stays custom", () => {
    expect(classifyAccess(["instance.read", "changes.prepare", "rows.update"], status)).toBe("custom");
    expect(classifyAccess([...baselineCapabilities(status, "manage"), "history.export"], status)).toBe("manage");
    expect(classifyAccess([...baselineCapabilities(status, "suggest"), "requests.send"], status)).toBe("custom");
  });

  it("an explicit Suggest switch removes acquisition while preserving independent privacy choices", () => {
    const current = [...baselineCapabilities(status, "manage"), "history.export", "history.providers", "requests.send"];
    expect(switchAccessMode(current, status, "suggest")).toEqual([
      "instance.read", "changes.prepare", "history.export", "history.providers",
    ]);
  });

  it("pins only newly selected current endpoints and leaves historical strings untouched", () => {
    const selections = [{ service_id: "searxng", destination_id: "http://localhost:8080" }];
    expect(selectedDestinationSnapshots(["https://old.example", "http://localhost:8080"], ["https://old.example"], selections)).toEqual([
      { service_id: "searxng", destination_id: "http://localhost:8080" },
    ]);
  });
});
