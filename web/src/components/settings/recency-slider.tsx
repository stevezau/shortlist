import { recencyDescription, recencyEras } from "@/lib/constants";
import { cn } from "@/lib/utils";

interface RecencySliderProps {
  id?: string;
  value: number; // whole percent, 0..100
  onChange: (pct: number) => void;
  className?: string;
}

/**
 * How much a title's RELEASE DATE counts when ranking it (0% = ignore age .. 100% = strongly prefer
 * new). A weight, not a filter — old titles still reach rows, they just have to be a better match.
 *
 * The era strip is the control's whole point. "50%" tells nobody anything, so the bars say what the
 * setting actually trades: how strongly a title of each vintage ranks, recomputed live from the
 * same curve the engine uses. Years are counted back from today rather than written down, so the
 * labels stay honest as the calendar moves.
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
  const eras = recencyEras(value, currentYear);

  return (
    <div className={cn("space-y-1.5", className)}>
      <div className="flex items-center gap-3">
        <input
          id={id}
          type="range"
          min={0}
          max={100}
          step={5}
          value={value}
          onChange={(e) => onChange(Number(e.target.value))}
          aria-label="How much a title's release date counts"
          aria-valuetext={`${value} percent towards recent releases`}
          className="h-2 w-full cursor-pointer accent-primary"
        />
        <span className="w-12 shrink-0 text-right text-sm font-medium tabular-nums">
          {value}%
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
                className="w-full rounded-sm bg-primary/70 transition-[height]"
                style={{ height: `${Math.max(3, era.weight * 100)}%` }}
              />
            </div>
            <span className="text-xs tabular-nums text-muted-foreground">
              {era.year}
            </span>
            <span className="text-xs tabular-nums text-muted-foreground/70">
              {Math.round(era.weight * 100)}%
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
