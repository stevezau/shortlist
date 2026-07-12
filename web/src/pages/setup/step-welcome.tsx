import { FakePlexRow } from "@/components/fake-plex-row";
import { Button } from "@/components/ui/button";

import type { StepProps } from "./step-props";

/**
 * Step 0 — a CSS-only mock of a Plex Home screen gaining a Picked-for-You
 * row (the animation loops; prefers-reduced-motion renders it static).
 */
export function StepWelcome({ next }: StepProps) {
  return (
    <div className="space-y-8">
      <div className="rounded-lg border bg-black/40 p-5">
        <p className="mb-4 text-xs uppercase tracking-widest text-muted-foreground">
          Your users' Plex Home, tonight
        </p>
        <div className="space-y-6">
          <FakePlexRow title="Continue Watching" posters={6} />
          <div className="motion-safe:animate-row-in">
            <FakePlexRow title="✨ Picked for You" posters={6} highlight />
          </div>
          <FakePlexRow title="Recently Added" posters={6} />
        </div>
      </div>

      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Every night, Rowarr reads what each of your users actually watched,
          asks a curator to pick what they should watch next from what you
          already own, and puts it on their Home screen — visible only to them.
        </p>
        <p className="text-sm text-muted-foreground">
          Setup takes about ten minutes, nothing is written to Plex until the
          built-in Privacy Check passes, and a full uninstall puts your server
          back exactly as Rowarr found it.
        </p>
      </div>

      <Button size="lg" onClick={next}>
        Connect Plex
      </Button>
    </div>
  );
}
