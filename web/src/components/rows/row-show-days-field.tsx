import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Segmented } from "@/components/segmented";
import {
  DAY_CHIPS,
  isoWeekday,
  type ShowDays,
  showDaysSentence,
} from "@/lib/show-days";

type Mode = "always" | "days";

/**
 * Which days this row appears on people's Home ("When it appears", issue #102).
 *
 * The pair with placement is the whole idea: placement is WHERE a row shows, this is WHEN. On its
 * off days the row is hidden rather than deleted, so it keeps its titles and returns without being
 * rebuilt.
 *
 * Controlled; emits ISO weekdays (1=Mon .. 7=Sun), or [] for every day.
 */
export function RowShowDaysField({
  value,
  onChange,
}: {
  value: ShowDays;
  onChange: (days: ShowDays) => void;
}) {
  const mode: Mode = value.length === 0 ? "always" : "days";

  const toggle = (iso: number) => {
    const next = value.includes(iso)
      ? value.filter((d) => d !== iso)
      : [...value, iso].sort((a, b) => a - b);
    // Deselecting the last day would mean "no days", which the API reads as EVERY day — the exact
    // opposite of what the click asked for. So the last one cannot be turned off; clearing the
    // schedule is what "Every day" is for, and it says so.
    if (next.length === 0) return;
    onChange(next);
  };

  const sentence = showDaysSentence(value);

  return (
    <div className="space-y-3">
      <Label>Show this row</Label>
      <Segmented<Mode>
        value={mode}
        ariaLabel="Which days this row appears"
        options={[
          { value: "always", label: "Every day" },
          { value: "days", label: "Only on these days" },
        ]}
        // Switching to "days" seeds today's weekday rather than an empty or arbitrary set: an empty
        // one means "every day" and would leave the control looking chosen but changing nothing.
        onChange={(next) =>
          onChange(next === "always" ? [] : [isoWeekday(new Date())])
        }
      />
      {mode === "days" && (
        <div
          className="flex flex-wrap gap-2"
          role="group"
          aria-label="Days this row appears"
        >
          {DAY_CHIPS.map((chip) => {
            const on = value.includes(chip.iso);
            return (
              <Button
                key={chip.iso}
                type="button"
                size="sm"
                variant={on ? "default" : "outline"}
                aria-pressed={on}
                className="min-w-14"
                onClick={() => toggle(chip.iso)}
              >
                {chip.short}
              </Button>
            );
          })}
        </div>
      )}
      <p className="text-sm text-muted-foreground">
        {sentence ||
          "This row appears every day. That is how every row behaves unless you change it."}
      </p>
      {mode === "days" && (
        <p className="text-sm text-muted-foreground">
          Days change at midnight on the server, and some Plex apps only notice
          once you leave the Home screen and come back.
        </p>
      )}
    </div>
  );
}
