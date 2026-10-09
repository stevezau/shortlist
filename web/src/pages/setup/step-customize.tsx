import { useMutation, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ArrowRight, Loader2, ShieldCheck } from "lucide-react";
import { useEffect, useId, useRef, useState } from "react";

import { ErrorState } from "@/components/query-boundary";
import { FakePlexRow } from "@/components/fake-plex-row";
import { RowSizeField } from "@/components/row-size-field";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api";
import { ROW_SIZE_DEFAULT } from "@/lib/constants";
import { renderRowName, settingString } from "@/lib/format";
import { queryKeys, useSettings } from "@/lib/queries";
import { cn } from "@/lib/utils";

import type { StepProps } from "./step-props";

const STATIC_TPL = "✨ {library_name} Picked for You";
const DYNAMIC_TPL = "Because you watched {top_seed}";
const EMOJI_CHOICES = ["✨", "🍿", "🎬", "⭐", "🔥", "❤️"];

type TemplateChoice = "static" | "dynamic" | "custom";

/**
 * Step 6 — row name template with a live fake-Plex-row preview, row size,
 * and the schedule time (design doc §3 step 6). Writes settings on save.
 */
export function StepCustomize({ update, next, back }: StepProps) {
  const queryClient = useQueryClient();
  const [choice, setChoice] = useState<TemplateChoice>("static");
  const [customTpl, setCustomTpl] = useState("✨ Fresh picks");
  const [rowSize, setRowSize] = useState(ROW_SIZE_DEFAULT);
  const customId = useId();

  // Re-entering this step (Back/Next) remounts it — the wizard swaps the component per step — so
  // seed the fields from what is already saved, once, when settings arrive. Without this the three
  // fields above silently reset to their literals and the next save wrote those defaults OVER the
  // owner's stored values. The worst case was "Skip for now — you can change this later", which is
  // also a save: a control whose label promises nothing changes was replacing a custom row name
  // with the classic default. Same pattern as `step-history`, for the same reason.
  const settings = useSettings();
  const seeded = useRef(false);
  useEffect(() => {
    const saved = settings.data;
    if (seeded.current || !saved) return;
    seeded.current = true;
    const savedTpl = settingString(saved, "row.name_template");
    const savedSize = saved["row.size"];
    // Functional updaters: if the fetch was slow and the owner already picked something, their
    // choice wins. An absent saved value never overwrites what they chose.
    //
    // All three called unconditionally at the effect's top level, exactly as `step-history` does.
    // Wrapping them in `if (savedTpl)` reads more naturally but trips
    // `react-hooks/set-state-in-effect`, which is an ERROR in this config and would fail CI's lint
    // job — so the "is there anything to apply?" test lives inside each updater instead.
    setChoice((cur) =>
      cur !== "static" || !savedTpl
        ? cur
        : savedTpl === DYNAMIC_TPL
          ? "dynamic"
          : savedTpl === STATIC_TPL
            ? "static"
            : "custom",
    );
    setCustomTpl((cur) =>
      !savedTpl || savedTpl === STATIC_TPL || savedTpl === DYNAMIC_TPL
        ? cur
        : savedTpl,
    );
    setRowSize((cur) =>
      typeof savedSize === "number" && savedSize > 0 && cur === ROW_SIZE_DEFAULT
        ? savedSize
        : cur,
    );
  }, [settings.data]);

  const template =
    choice === "static"
      ? STATIC_TPL
      : choice === "dynamic"
        ? DYNAMIC_TPL
        : customTpl;

  const save = useMutation({
    mutationFn: () =>
      api.putSettings({
        "row.name_template": template,
        "row.size": rowSize,
      }),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.settings });
      update({ customized: true });
      next();
    },
  });

  const templateOptions: { id: TemplateChoice; label: string; hint: string }[] =
    [
      {
        id: "static",
        label: renderRowName(STATIC_TPL),
        hint: "Classic — uses each library's own name.",
      },
      {
        id: "dynamic",
        // Rendered, not raw. Its sibling above calls renderRowName and this did not, so the card
        // showed "Because you watched {top_seed}" — template syntax presented as finished copy.
        label: renderRowName(DYNAMIC_TPL),
        hint: "Renamed each night after whatever they watched most recently.",
      },
      { id: "custom", label: "Custom…", hint: "Your words, your emoji." },
    ];

  return (
    <div className="space-y-8">
    <div className="grid grid-cols-1 items-start gap-7 md:grid-cols-[minmax(0,.95fr)_minmax(0,1.05fr)] md:gap-9">
      <div className="min-w-0 space-y-6">
      {settings.isError && <ErrorState error={settings.error} onRetry={() => void settings.refetch()} />}
      <fieldset className="space-y-3">
        <legend className="text-xs font-semibold uppercase tracking-widest text-muted-foreground">Row name</legend>
        <div className="grid gap-2">
          {templateOptions.map((option) => (
            <button
              key={option.id}
              type="button"
              onClick={() => setChoice(option.id)}
              aria-pressed={choice === option.id}
              className={cn(
                "flex min-h-18 items-start gap-3 rounded-lg border bg-card p-3.5 text-left transition-colors hover:border-primary/40 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring",
                choice === option.id && "border-primary/60 bg-gradient-to-r from-primary/15 to-primary/5",
              )}
            >
              <span aria-hidden="true" className={cn("mt-1 grid size-4 shrink-0 place-items-center rounded-full border", choice === option.id ? "border-primary" : "border-muted-foreground/50")}>
                {choice === option.id && <span className="size-2 rounded-full bg-primary" />}
              </span>
              <div className="min-w-0 flex-1 space-y-1">
                  <p className="text-sm font-medium leading-snug">
                    {option.label}
                  </p>
                  <p className="text-xs text-muted-foreground">{option.hint}</p>
              </div>
              {option.id === "static" && <span className="pt-1 text-xs font-medium uppercase tracking-wide text-primary/80">Classic</span>}
            </button>
          ))}
        </div>
      </fieldset>

      {choice === "custom" && (
        <div className="space-y-2">
          <Label htmlFor={customId}>Custom row name</Label>
          <Input
            id={customId}
            value={customTpl}
            onChange={(event) => setCustomTpl(event.target.value)}
          />
          <div className="flex flex-wrap gap-1">
            {EMOJI_CHOICES.map((emoji) => (
              <Button
                key={emoji}
                type="button"
                variant="outline"
                size="sm"
                aria-label={`Add ${emoji}`}
                onClick={() => setCustomTpl((current) => `${current}${emoji}`)}
              >
                {emoji}
              </Button>
            ))}
          </div>
          <p className="text-xs text-muted-foreground">
            Tip: <span className="font-mono">{"{library_name}"}</span> becomes
            each library's name (Movies, TV Shows…),{" "}
            <span className="font-mono">{"{user}"}</span> each person's name,
            and <span className="font-mono">{"{top_seed}"}</span> their top
            watched title, fresh every night.
          </p>
        </div>
      )}

      <RowSizeField value={rowSize} onChange={setRowSize} presets={[10, 15, 20]} />

      <p className="hidden text-xs leading-relaxed text-muted-foreground md:block">
        Starts with a nightly refresh. Change the name and size later in Settings,
        or change the schedule and switch the row off in its editor.
      </p>
      <p className="text-xs text-muted-foreground md:hidden">You can change these choices later in Settings.</p>
      </div>
      <div className="min-w-0 space-y-4 md:sticky md:top-8">
      <section aria-label="Preview on Plex" className="overflow-hidden rounded-xl border bg-gradient-to-br from-primary/5 via-card to-background">
        <div className="flex items-center justify-between border-b px-5 py-4">
          <p className="text-xs uppercase tracking-widest text-muted-foreground">Live preview</p>
          <span className="text-xs text-primary/75">On Plex</span>
        </div>
        <div className="space-y-3 px-5 pb-5 pt-6">
        <p className="text-xs uppercase tracking-wide text-muted-foreground">Home · Movies</p>
        <FakePlexRow
          title={renderRowName(template) || STATIC_TPL}
          illustrative
        />
        <div className="flex justify-between gap-3 text-xs text-muted-foreground"><span>Showing 4 of {rowSize} titles</span><span>Illustrative picks</span></div>
        </div>
        <p className="border-t bg-background/60 px-5 py-4 text-xs leading-relaxed text-muted-foreground">
          Each person gets their own recommendations. This example previews the name;
          real artwork and picks come from their library after the first run.
          <span className="md:hidden"> Starts nightly; change the schedule or switch the row off later in its editor.</span>
        </p>
      </section>
      <p className="flex items-start gap-2 text-xs leading-relaxed text-muted-foreground"><ShieldCheck aria-hidden="true" className="mt-0.5 size-3.5 shrink-0" />Row names do not change your Plex sharing settings.</p>
      </div>
    </div>
    <footer className="sticky bottom-0 z-10 space-y-2 border-t bg-background/95 py-4 backdrop-blur-sm md:static md:pt-5">
      <div className="flex items-center justify-between gap-3">
        {back ? <Button variant="ghost" onClick={back}><ArrowLeft aria-hidden="true" />Back</Button> : <span />}
        <Button onClick={() => save.mutate()} disabled={save.isPending || settings.isPending || settings.isError}>
          {save.isPending && <Loader2 className="animate-spin" aria-hidden="true" />}
          Save & continue<ArrowRight aria-hidden="true" className="ml-3" />
        </Button>
      </div>
      {save.isError ? <p role="alert" className="text-sm text-destructive-text">{apiErrorMessage(save.error, "Saving failed. Try again.")}</p> : <p className="text-right text-xs text-muted-foreground">Saves these choices, then opens your first run.</p>}
    </footer>
    </div>
  );
}
