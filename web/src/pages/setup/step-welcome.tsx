import { RotateCcw, Sparkles } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";

import type { StepProps } from "./step-props";

const PROMISES = [
  {
    icon: Sparkles,
    title: "A row of their own",
    body: "Every user gets a “✨ Picked for You” row on their Plex Home, built from what they actually watched — with sharing rules that keep personal rows separate.",
  },
  {
    icon: RotateCcw,
    title: "Reversible",
    // "Snapshotted" is a word from the code, on the first screen of the wizard — the one place the
    // reader has least context for it.
    body: "Before Shortlist changes any of your Plex sharing settings, it saves a copy of them exactly as they were. Uninstall puts your server back the way it found it.",
  },
];

/** Step 0 — what this actually does, and what it promises. Then one button. */
export function StepWelcome({ next }: StepProps) {
  return (
    <div className="space-y-8">
      <div className="grid gap-3 sm:grid-cols-2">
        {PROMISES.map(({ icon: Icon, title, body }) => (
          <Card key={title}>
            <CardContent className="space-y-2 pt-6">
              <span
                aria-hidden="true"
                className="inline-grid h-9 w-9 place-items-center rounded-lg border bg-elevated text-primary"
              >
                <Icon className="h-5 w-5" />
              </span>
              <p className="font-medium">{title}</p>
              <p className="text-sm text-muted-foreground">{body}</p>
            </CardContent>
          </Card>
        ))}
      </div>

      <p className="text-sm text-muted-foreground">Plex cannot hide other people’s rows from the server owner on every surface. Managed accounts with parental profiles also have limitations; setup explains these before your first run.</p>

      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Shortlist uses a free TMDB key to discover titles and Plex to build
          rows in your own library. It needs no AI subscription. Adding an AI provider (Claude, GPT, Gemini, or one you run
          yourself) is optional &mdash; it unlocks rows built from a live web
          search, and AI-drawn artwork.
        </p>
        <Button size="lg" onClick={next}>
          Get started
        </Button>
      </div>
    </div>
  );
}
