import { useMutation } from "@tanstack/react-query";
import { Check, Loader2 } from "lucide-react";
import { useId, useState } from "react";

import { FakePlexRow } from "@/components/fake-plex-row";
import { RowSizeField } from "@/components/row-size-field";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { api, apiErrorMessage } from "@/lib/api";
import { ROW_SIZE_DEFAULT } from "@/lib/constants";
import { cronFromTime, renderRowName } from "@/lib/format";
import { cn } from "@/lib/utils";

import type { StepProps } from "./step-props";

const STATIC_TPL = "✨ Picked for You";
const DYNAMIC_TPL = "Because you watched {top_seed}";
const EMOJI_CHOICES = ["✨", "🍿", "🎬", "⭐", "🔥", "❤️"];

type TemplateChoice = "static" | "dynamic" | "custom";

/**
 * Step 6 — row name template with a live fake-Plex-row preview, row size,
 * and the schedule time (design doc §3 step 6). Writes settings on save.
 */
export function StepCustomize({ update, next }: StepProps) {
  const [choice, setChoice] = useState<TemplateChoice>("static");
  const [customTpl, setCustomTpl] = useState("✨ Fresh picks");
  const [rowSize, setRowSize] = useState(ROW_SIZE_DEFAULT);
  const [time, setTime] = useState("03:30");
  const customId = useId();
  const timeId = useId();

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
        "schedule.cron": cronFromTime(time),
      }),
    onSuccess: () => {
      update({ customized: true });
      next();
    },
  });

  const templateOptions: { id: TemplateChoice; label: string; hint: string }[] =
    [
      { id: "static", label: STATIC_TPL, hint: "Classic, always the same." },
      {
        id: "dynamic",
        label: DYNAMIC_TPL,
        hint: "Rewritten nightly from each user's top seed.",
      },
      { id: "custom", label: "Custom…", hint: "Your words, your emoji." },
    ];

  return (
    <div className="space-y-6">
      <fieldset className="space-y-2">
        <legend className="text-sm font-medium">Row name</legend>
        <div className="grid gap-2 sm:grid-cols-3">
          {templateOptions.map((option) => (
            <button
              key={option.id}
              type="button"
              onClick={() => setChoice(option.id)}
              aria-pressed={choice === option.id}
              className={cn(
                "rounded-lg text-left",
                choice === option.id && "ring-2 ring-primary",
              )}
            >
              <Card className="h-full">
                <CardContent className="space-y-1 p-4">
                  <p className="flex items-center justify-between text-sm font-medium">
                    {option.label}
                    {choice === option.id && (
                      <Check
                        className="h-4 w-4 text-primary"
                        aria-hidden="true"
                      />
                    )}
                  </p>
                  <p className="text-xs text-muted-foreground">{option.hint}</p>
                </CardContent>
              </Card>
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
          <div className="flex gap-1">
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
            Tip: <span className="font-mono">{"{top_seed}"}</span> becomes each
            user's top watched title, fresh every night.
          </p>
        </div>
      )}

      <div className="rounded-lg border bg-black/40 p-5">
        <p className="mb-3 text-xs uppercase tracking-widest text-muted-foreground">
          Live preview
        </p>
        <FakePlexRow
          title={renderRowName(template) || STATIC_TPL}
          posters={Math.min(rowSize, 8)}
          highlight
        />
        <p className="mt-3 text-xs text-muted-foreground">
          This previews the row&rsquo;s <em>title</em> as it&rsquo;ll appear on
          Plex — the &ldquo;Because you watched&hellip;&rdquo; option even fills
          in a real example. The tiles are placeholders: the actual posters come
          from each person&rsquo;s own library after the first run.
        </p>
      </div>

      <RowSizeField value={rowSize} onChange={setRowSize} />

      <div className="space-y-2">
        <Label htmlFor={timeId}>Refresh rows nightly at</Label>
        <Input
          id={timeId}
          type="time"
          value={time}
          onChange={(event) => setTime(event.target.value)}
          className="w-32"
        />
        <p className="text-sm text-muted-foreground">
          Server-local time. Weekly cadence, cron expressions, and per-user
          schedules live in Settings → Schedules once you're set up.
        </p>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button onClick={() => save.mutate()} disabled={save.isPending}>
          {save.isPending && (
            <Loader2 className="animate-spin" aria-hidden="true" />
          )}
          Save & continue
        </Button>
        {/* Skip also SAVES the current values (they're always valid — every field is pre-filled), so a
            name or size the user typed is never silently dropped by taking the quick path out. It's a
            softer-worded alias for the same save; everything can still be changed later in Settings. */}
        <Button
          variant="ghost"
          onClick={() => save.mutate()}
          disabled={save.isPending}
        >
          Skip for now — you can change this later
        </Button>
      </div>
      {save.isError && (
        <p role="alert" className="text-sm text-destructive">
          {apiErrorMessage(save.error, "Saving failed. Try again.")}
        </p>
      )}
    </div>
  );
}
