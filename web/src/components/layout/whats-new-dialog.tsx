import { ExternalLink } from "lucide-react";
import { lazy, Suspense, useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Skeleton } from "@/components/ui/skeleton";
import { formatDate } from "@/lib/format";
import { useMarkWhatsNewSeen, useWhatsNew } from "@/lib/queries";

// The markdown renderer is ~116 kB, and this dialog is in the app shell, so a static import put it in
// every first paint for a pop-up that opens at most once per release. Loaded with the dialog instead.
const ReleaseNotes = lazy(() =>
  import("@/components/layout/release-notes").then((module) => ({
    default: module.ReleaseNotes,
  })),
);

function NotesLoading() {
  return (
    <div role="status" className="space-y-2">
      <span className="sr-only">Loading the release notes…</span>
      <Skeleton className="h-4 w-1/3" />
      <Skeleton className="h-4 w-full" />
      <Skeleton className="h-4 w-5/6" />
    </div>
  );
}

/**
 * The release notes, shown once to the owner after an upgrade.
 *
 * Closing it in any way — the button, the X, Escape, a click outside — records it on the server, so it
 * stays closed across reloads, restarts and other browsers until a newer release arrives. It renders
 * nothing while loading or on an error: the notes are a courtesy, not something to act on, and a
 * pop-up announcing that GitHub is unreachable would be worse than no pop-up.
 *
 * The notes are the release's markdown from GitHub, rendered by `release-notes.tsx`.
 */
export function WhatsNewDialog() {
  const whatsNew = useWhatsNew();
  const markSeen = useMarkWhatsNewSeen();
  const [closedVersion, setClosedVersion] = useState<string | null>(null);

  const releases = whatsNew.data?.releases ?? [];
  const newest = releases[0];
  if (!newest) return null;

  // The newest release SHOWN, not the running build: a build can run before CI publishes its
  // release, and recording that version would mark notes read that nobody has seen.
  const close = () => {
    setClosedVersion(newest.version);
    markSeen.mutate(newest.version);
  };

  return (
    <Dialog
      open={closedVersion !== newest.version}
      onOpenChange={(open) => !open && close()}
    >
      <DialogContent className="flex max-h-[85vh] flex-col sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>What&rsquo;s new in Shortlist {newest.version}</DialogTitle>
          <DialogDescription>
            {releases.length === 1
              ? `Released ${formatDate(newest.published_at, { dateOnly: true })}.`
              : `${releases.length} releases since you last read these notes.`}
          </DialogDescription>
        </DialogHeader>

        <div className="-mx-6 min-h-0 flex-1 space-y-6 overflow-y-auto border-y px-6 py-4">
          <Suspense fallback={<NotesLoading />}>
            {releases.map((release) => (
              <section
                key={release.version}
                className="space-y-3 text-sm text-muted-foreground"
              >
                {releases.length > 1 && (
                  <div className="flex flex-wrap items-baseline gap-x-2">
                    <h3 className="text-base font-semibold text-foreground">
                      Shortlist {release.version}
                    </h3>
                    <p className="text-xs">
                      {formatDate(release.published_at, { dateOnly: true })}
                    </p>
                  </div>
                )}
                <ReleaseNotes markdown={release.notes} />
              </section>
            ))}
          </Suspense>
        </div>

        <DialogFooter className="gap-2 sm:items-center">
          <Button variant="ghost" asChild>
            <a href={newest.url} target="_blank" rel="noopener noreferrer">
              View on GitHub
              <ExternalLink aria-hidden="true" />
            </a>
          </Button>
          <Button onClick={close}>Got it</Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
