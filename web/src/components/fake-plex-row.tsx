import { cn } from "@/lib/utils";

const POSTER_TONES = [
  "from-zinc-700 to-zinc-800",
  "from-stone-700 to-zinc-800",
  "from-neutral-700 to-stone-800",
  "from-zinc-600 to-neutral-800",
  "from-stone-600 to-zinc-700",
  "from-neutral-600 to-zinc-800",
];

// Deliberately illustrative, so the setup preview never implies these are someone's real picks.
const EXAMPLE_POSTERS = [
  { title: "Arrival", background: "radial-gradient(ellipse at 50% 33%, #c1baa4 0%, #777e77 17%, #3b4947 18%, #354444 43%, #151b1d 78%)" },
  { title: "Dune", background: "linear-gradient(156deg, #1d2431 15%, #484956 45%, #ca9d6b 46%, #ab7845 58%, #432c24 100%)" },
  { title: "Blade Runner 2049", background: "linear-gradient(35deg, #211727, #25333a 45%, #32676a 47%, #171d2b 73%)" },
  { title: "Her", background: "radial-gradient(circle at 60% 35%, #d5b0a0 0%, #966461 18%, #472e3b 38%, #201d2b 80%)" },
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
  illustrative = false,
}: {
  title: string;
  posters?: number;
  highlight?: boolean;
  className?: string;
  illustrative?: boolean;
}) {
  return (
    <div className={cn(illustrative ? "space-y-4" : "space-y-2", className)}>
      <p
        className={cn(
          "font-semibold",
          illustrative ? "text-lg leading-snug text-foreground" : "text-sm",
          !illustrative && (highlight ? "text-primary" : "text-muted-foreground"),
        )}
      >
        {title}
      </p>
      <div aria-hidden="true" className={illustrative ? "grid grid-cols-4 gap-2" : "flex gap-2 overflow-hidden"}>
        {illustrative ? EXAMPLE_POSTERS.map((poster) => (
          <div key={poster.title} style={{ background: poster.background }}
            className="relative flex aspect-[2/3] min-w-0 items-end overflow-hidden rounded-md border border-white/10 p-2 shadow-lg sm:p-3">
            <span className="absolute inset-0 bg-gradient-to-b from-transparent via-transparent to-black/70" />
            <span className="relative text-[9px] font-semibold uppercase leading-relaxed tracking-[0.12em] text-white/90 sm:text-[10px]">{poster.title}</span>
          </div>
        )) : Array.from({ length: posters }, (_, i) => (
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
