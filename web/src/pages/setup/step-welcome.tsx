import { Info, RotateCcw, Sparkles } from "lucide-react";

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

      {/* The one thing a new admin must know before pressing Get started, so it is a note of its own
          rather than a grey paragraph between the promises and the button. */}
      <div role="note" className="flex gap-3 rounded-lg border border-border-strong bg-elevated p-4 text-sm">
        <Info className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0 space-y-1">
          <p className="font-medium">Plex cannot hide other people’s rows from the server owner.</p>
          <p className="text-muted-foreground">
            Your own admin account sees everyone’s rows in places like each library’s Collections tab.
            Managed accounts with parental profiles have limits too; setup explains both before your first run.
          </p>
        </div>
      </div>

      <div className="space-y-3">
        <p className="text-sm text-muted-foreground">
          Needs only your Plex server and a free TMDB key; an AI provider is optional and adds web-search rows and AI-drawn artwork.
        </p>
        <Button size="lg" onClick={next}>
          Get started
        </Button>
      </div>
    </div>
  );
}
