import { recencyDescription, recencyEras } from "@/lib/constants";
import { cn } from "@/lib/utils";

interface RecencySliderProps {
  id?: string;
  value: number; // whole percent, -100 (older) .. 100 (newer)
  onChange: (pct: number) => void;
  className?: string;
}

/**
 * How much a title's RELEASE DATE counts when ranking it (-100% = lean older, 0% = ignore age, 100% = strongly
 * prefer new). A weight, not a filter — old titles still reach rows, they just have to be a better match.
 *
 * The era strip is the control's whole point. "50%" tells nobody anything, so the bars say what the
 * setting DOES to titles of each age: how strongly a title of each vintage ranks, recomputed live from
 * the same curve the engine uses. Each is relative to a title released this year, so the newest
 * (rightmost) bar is the reference: "this year" on the newer side, "+0%" on the older side. Years are
 * counted back from today rather than written down, so the labels stay honest as the calendar moves.
 *
 * A native range input, so it is keyboard-accessible for free; the whole-percent value maps to a
 * 0..1 fraction at the call site, like the watched-cap slider beside it.
 */
export function RecencySlider({
  id,
  value,
  onChange,
  className,
}: RecencySliderProps) {
  const currentYear = new Date().getFullYear();
  // Oldest on the left, newest on the right — the same direction as the slider's Older / Newer ends.
  const eras = recencyEras(value, currentYear).reverse();

  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="flex items-start gap-3">
        <div className="min-w-0 flex-1 space-y-1.5">
        <input
          id={id}
          type="range"
          min={-100}
          max={100}
          step={5}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          aria-label="How much a title's release date counts"
          aria-valuetext={
            value < 0
              ? `${-value} percent towards older releases`
              : `${value} percent towards recent releases`
          }
          className="h-2 w-full cursor-pointer accent-primary"
        />
        {/* Under the track itself, not the whole row, so "Ignore age" sits under the track's centre
            whatever width the value beside it takes. Zero is a step, so it is a real detent. */}
        <div
          aria-hidden="true"
          className="flex justify-between text-xs text-muted-foreground"
        >
          <span>Older</span>
          <span>Ignore age</span>
          <span>Newer</span>
        </div>
        </div>
        <span className="min-w-12 shrink-0 text-right text-sm leading-none font-medium tabular-nums">
          {value < 0 ? `${-value}% older` : `${value}%`}
        </span>
      </div>
      {/* Presentation only: the sentence below carries the same information for screen readers, so
          the bars are hidden from the accessibility tree rather than read out as a row of numbers. */}
      <div aria-hidden="true" className="flex items-end gap-2 pt-1">
        {eras.map((era) => (
          <div
            key={era.year}
            className="flex flex-1 flex-col items-center gap-1"
          >
            {/* Capped width, not `w-full`: stretched across a settings card each bar is ~230px
                wide and 40px tall, which reads as a progress bar rather than a column in a chart.
                The floor keeps a near-zero weight visible as a sliver instead of vanishing. */}
            <div className="flex h-16 w-full max-w-10 items-end">
              <div
                className="w-full rounded-sm bg-foreground/30 transition-[height]"
                style={{ height: `${Math.max(3, era.weight * 100)}%` }}
              />
            </div>
            <span className="text-xs tabular-nums text-muted-foreground">
              {era.year}
            </span>
            {/* The newest era is the REFERENCE every other bar is measured against, so its weight is
                1 at every slider position. Printing "100%" on a number that can never move reads as
                a value that ought to respond to the control — so it is named for what it is. */}
            <span className="text-xs tabular-nums text-muted-foreground/70">
              {value < 0
                ? `+${Math.round((era.factor - 1) * 100)}%`
                : era.weight >= 1
                  ? "this year"
                  : `${Math.round(era.weight * 100)}%`}
            </span>
          </div>
        ))}
      </div>
      <p className="text-sm text-muted-foreground">
        {recencyDescription(value, currentYear)}
      </p>
    </div>
  );
}
