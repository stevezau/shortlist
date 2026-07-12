import { cn } from "@/lib/utils";

const POSTER_TONES = [
  "from-zinc-700 to-zinc-800",
  "from-stone-700 to-zinc-800",
  "from-neutral-700 to-stone-800",
  "from-zinc-600 to-neutral-800",
  "from-stone-600 to-zinc-700",
  "from-neutral-600 to-zinc-800",
];

/**
 * A row as it would appear on a Plex Home screen — used for the welcome-step
 * mock and the live row-name preview in customization.
 */
export function FakePlexRow({
  title,
  posters = 6,
  highlight = false,
  className,
}: {
  title: string;
  posters?: number;
  highlight?: boolean;
  className?: string;
}) {
  return (
    <div className={cn("space-y-2", className)}>
      <p
        className={cn(
          "text-sm font-semibold",
          highlight ? "text-primary" : "text-muted-foreground",
        )}
      >
        {title}
      </p>
      <div aria-hidden="true" className="flex gap-2 overflow-hidden">
        {Array.from({ length: posters }, (_, i) => (
          <div
            key={i}
            className={cn(
              "h-20 w-14 shrink-0 rounded-sm bg-gradient-to-br",
              POSTER_TONES[i % POSTER_TONES.length],
            )}
          />
        ))}
      </div>
    </div>
  );
}
