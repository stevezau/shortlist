import { useId, useState, type Dispatch, type SetStateAction } from "react";

import { Input } from "@/components/ui/input";
import { switchAccessMode, type AssistantMode, type SelectedDestination } from "@/lib/assistant-permission-model";
import type { AssistantDestination, AssistantGrantConstraints, AssistantStatus } from "@/lib/types";

function setCapability(values: string[], capability: string, enabled: boolean): string[] {
  return enabled ? [...new Set([...values, capability])] : values.filter((value) => value !== capability);
}

export function AssistantPermissionFields({
  status,
  mode,
  setMode,
  capabilities,
  setCapabilities,
  constraints,
  setConstraints,
  choices,
  setSelectedDestinations,
  reserved = 0,
  requestedScopes,
  groupsCustomized = false,
}: {
  status: AssistantStatus;
  mode: AssistantMode;
  setMode: (mode: AssistantMode) => void;
  capabilities: string[];
  setCapabilities: Dispatch<SetStateAction<string[]>>;
  constraints: AssistantGrantConstraints;
  setConstraints: Dispatch<SetStateAction<AssistantGrantConstraints>>;
  choices: AssistantDestination[];
  setSelectedDestinations: Dispatch<SetStateAction<SelectedDestination[]>>;
  reserved?: number;
  requestedScopes?: string[];
  groupsCustomized?: boolean;
}) {
  const modeHelpId = useId();
  const [paidChanged, setPaidChanged] = useState(false);
  const [paidEnabled, setPaidEnabled] = useState(constraints.max_provider_calls > 0);
  const [paidInput, setPaidInput] = useState(String(constraints.max_provider_calls));
  const paidPermission = capabilities.includes("ai.generate");
  const remaining = Math.max(0, constraints.max_provider_calls - reserved);
  const requested = (capability: string) => !requestedScopes || requestedScopes.includes(capability);
  const manageRequested = !requestedScopes || requestedScopes.some((scope) =>
    status.presets.owner_automation.includes(scope) && !status.presets.inspect.includes(scope));
  const suggestRequested = !requestedScopes || status.presets.inspect.some((scope) => requestedScopes.includes(scope));

  function chooseMode(next: "suggest" | "manage") {
    setCapabilities((value) => switchAccessMode(value, status, next));
    setMode(next);
    if (!groupsCustomized) {
      setConstraints((value) => ({
        ...value,
        setting_groups: next === "manage" ? [...status.setting_groups] : [],
      }));
    }
  }

  function choosePaid(enabled: boolean) {
    if (enabled && reserved >= 100) return;
    setPaidChanged(true);
    setPaidEnabled(enabled);
    const nextLimit = enabled ? Math.max(1, reserved + 1) : 0;
    setPaidInput(String(nextLimit));
    setCapabilities((value) => setCapability(value, "ai.generate", enabled));
    setConstraints((value) => ({ ...value, max_provider_calls: nextLimit }));
  }

  function chooseService(choice: AssistantDestination) {
    const alreadySelected = constraints.destination_ids.includes(choice.destination_id);
    setConstraints((value) => ({
      ...value,
      destination_ids: alreadySelected
        ? value.destination_ids.filter((id) => id !== choice.destination_id)
        : [...value.destination_ids, choice.destination_id],
    }));
    setSelectedDestinations((value) => alreadySelected
      ? value.filter((selection) => selection.destination_id !== choice.destination_id)
      : [...value, { service_id: choice.service_id, destination_id: choice.destination_id }]);
  }

  return (
    <>
      <section className="space-y-3">
        <div><h2 className="font-semibold">What this assistant can do</h2><p className="text-sm text-muted-foreground">All current and future people. {constraints.include_future_rows ? "All current and future rows" : `${constraints.row_ids.length} selected rows`}; {constraints.include_future_libraries ? "all current and future libraries" : `${constraints.library_keys.length} selected libraries`}. Row audiences still decide who sees each row.</p></div>
        {mode === "custom" && <p className="text-sm text-muted-foreground">Custom access and existing resource limits are in force. Choosing a mode below replaces its main permissions.</p>}
        <div className="grid gap-2 sm:grid-cols-2">
          {(["suggest", "manage"] as const).map((option) => <button key={option} type="button" aria-label={option === "manage" ? "Manage Shortlist" : "Suggest changes"} aria-describedby={`${modeHelpId}-${option}`} aria-pressed={mode === option} disabled={option === "manage" ? !manageRequested : !suggestRequested} onClick={() => chooseMode(option)} className={`rounded-md border p-3 text-left text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 ${mode === option ? "border-primary bg-raised shadow-selected-y" : "bg-elevated hover:bg-raised"}`}>
            <span className="block font-semibold">{option === "manage" ? "Manage Shortlist" : "Suggest changes"}</span>
            <span id={`${modeHelpId}-${option}`} className="mt-1 block text-xs text-muted-foreground">{option === "manage" ? requestedScopes ? "Use only the management permissions this assistant requested." : "Set up and operate Shortlist within this connection's limits." : "Read safe information and prepare changes for your review."}</span>
          </button>)}
        </div>
        {mode === "suggest" && capabilities.includes("requests.send") && <p className="text-xs text-muted-foreground">Choosing Suggest changes turns off requests for missing titles.</p>}
        {requestedScopes && mode === "suggest" && !requestedScopes.some((scope) => status.presets.owner_automation.includes(scope) && !status.presets.inspect.includes(scope)) && <p className="text-xs text-muted-foreground">This assistant requested suggestion permissions only. It must request management permissions before it can manage Shortlist.</p>}
      </section>

      <section className="space-y-2">
        <h2 className="font-semibold">Privacy</h2>
        <label className="flex items-start gap-3 py-1 text-sm"><input type="checkbox" checked={capabilities.includes("history.export")} disabled={!requested("history.export")} onChange={(event) => setCapabilities((value) => setCapability(value, "history.export", event.target.checked))} className="mt-1 h-4 w-4 accent-primary" /><span>Show viewing details to this assistant</span></label>
        <label className="flex items-start gap-3 py-1 text-sm"><input type="checkbox" checked={capabilities.includes("history.providers")} disabled={!requested("history.providers")} onChange={(event) => setCapabilities((value) => setCapability(value, "history.providers", event.target.checked))} className="mt-1 h-4 w-4 accent-primary" /><span>Allow viewing context to approved services</span></label>
      </section>

      <section className="space-y-2">
        <label className="flex items-start gap-3 text-sm"><input type="checkbox" checked={paidEnabled} disabled={!requested("ai.generate") || (!paidEnabled && reserved >= 100)} onChange={(event) => choosePaid(event.target.checked)} className="mt-1 h-4 w-4 accent-primary" /><span className="font-medium">Allow this assistant to use paid services</span></label>
        {paidEnabled && <div className="max-w-sm space-y-2 pl-7 text-sm">
          <label className="block space-y-1"><span>Lifetime call allowance</span><Input type="number" required min={paidChanged ? reserved + 1 : 0} max={100} value={paidInput} onChange={(event) => {
            setPaidChanged(true);
            setPaidInput(event.target.value);
            setCapabilities((value) => setCapability(value, "ai.generate", true));
            setConstraints((value) => ({ ...value, max_provider_calls: Number(event.target.value) }));
          }} /></label>
          <p className="text-xs text-muted-foreground">Used or uncertain: {reserved} · Remaining now: {remaining}{remaining === 0 ? " · Exhausted" : ""}</p>
        </div>}
        {!paidEnabled && reserved > 0 && <p className="pl-7 text-xs text-muted-foreground">Used or uncertain: {reserved} · Remaining: 0</p>}
        {paidEnabled !== paidPermission && <p className="text-xs text-muted-foreground">This older connection has a mismatched paid permission and allowance. Its existing access stays unchanged until you change this choice.</p>}
        <p className="text-xs text-muted-foreground">For direct assistant calls. Scheduled Shortlist runs use their existing settings.</p>
      </section>

      {(mode === "manage" || capabilities.includes("requests.send")) && <section>
        <label className="flex items-start gap-3 text-sm"><input type="checkbox" checked={capabilities.includes("requests.send")} disabled={!requested("requests.send")} onChange={(event) => setCapabilities((value) => setCapability(value, "requests.send", event.target.checked))} className="mt-1 h-4 w-4 accent-primary" /><span><span className="block font-medium">Allow requests for missing titles</span><span className="block text-xs text-muted-foreground">Send approved title requests to a selected request service.</span></span></label>
      </section>}

      <section className="space-y-2">
        <div><h2 className="font-semibold">Services this assistant can use</h2><p className="text-xs text-muted-foreground">Choose services for connection checks, search, AI or requests. Selection approves each service's current endpoint.</p></div>
        {choices.length > 0 && <div className="flex gap-3 text-xs"><button type="button" onClick={() => {
          setConstraints((value) => ({ ...value, destination_ids: [...new Set([...value.destination_ids, ...choices.map((choice) => choice.destination_id)])] }));
          setSelectedDestinations((value) => [...value, ...choices.filter((choice) => !value.some((selection) => selection.service_id === choice.service_id && selection.destination_id === choice.destination_id)).map(({ service_id, destination_id }) => ({ service_id, destination_id }))]);
        }} className="text-primary hover:underline">Select all current services</button><button type="button" onClick={() => {
          setConstraints((value) => ({ ...value, destination_ids: value.destination_ids.filter((id) => !choices.some((choice) => choice.destination_id === id)) }));
          setSelectedDestinations((value) => value.filter((selection) => !choices.some((choice) => choice.destination_id === selection.destination_id)));
        }} className="text-primary hover:underline">Clear current services</button></div>}
        {choices.map((choice) => <label key={choice.service_id} className="flex items-start gap-3 py-1 text-sm"><input type="checkbox" checked={constraints.destination_ids.includes(choice.destination_id)} onChange={() => chooseService(choice)} className="mt-1 h-4 w-4 accent-primary" /><span><span className="block font-medium">{choice.label}</span><span className="block text-xs text-muted-foreground">{choice.host}</span></span></label>)}
        {choices.length === 0 && <p className="text-xs text-muted-foreground">Configure a supported service in Settings before approving it here.</p>}
      </section>
    </>
  );
}
