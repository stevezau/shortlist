import { watchedPctDescription } from "@/lib/constants";
import { cn } from "@/lib/utils";

interface WatchedSliderProps {
  id?: string;
  value: number; // whole percent, 0..100
  onChange: (pct: number) => void;
  className?: string;
}

/**
 * The already-watched cap as a real slider (0% = all fresh .. 100% = no filtering). A native range
 * input so it's keyboard-accessible for free; the whole-percent value maps to a 0..1 fraction at
 * the call site.
 */
export function WatchedSlider({
  id,
  value,
  onChange,
  className,
}: WatchedSliderProps) {
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
          aria-label="Maximum share of the row that may be already-watched"
          aria-valuetext={`${value} percent already watched`}
          className="h-2 w-full cursor-pointer accent-primary"
        />
        <span className="w-12 shrink-0 text-right text-sm font-medium tabular-nums">
          {value}%
        </span>
      </div>
      <p className="text-sm text-muted-foreground">
        {watchedPctDescription(value)}
      </p>
    </div>
  );
}
