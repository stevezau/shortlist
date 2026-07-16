import { useMutation } from "@tanstack/react-query";
import { ShieldCheck } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { api, apiErrorMessage } from "@/lib/api";

/** The manual Privacy Check controls: the fast read-only pass, and the full ~90s probe. */
export function PrivacySection() {
  // The read-only check (T1 filter read-back + T2 canary view) is seconds and touches nothing.
  // The full probe creates and removes a throwaway collection — the same proof the wizard runs.
  const privacyCheck = useMutation({
    mutationFn: (probe: boolean) => api.runPrivacyCheck({ probe }),
  });

  return (
    <section aria-labelledby="privacy-heading" className="space-y-3">
      <h2 id="privacy-heading" className="text-lg font-semibold">
        Privacy
      </h2>
      <Card>
        <CardContent className="space-y-4 pt-6">
          <p className="text-sm text-muted-foreground">
            Shortlist will not write to Plex unless a Privacy Check has passed
            in the last seven days. The quick check reads every user&rsquo;s
            share filters back from plex.tv and looks at what one of your
            users&rsquo; accounts can actually see on their own Home. The full
            check goes further: it creates a throwaway test row, proves it stays
            hidden from that stand-in account, and removes it again.
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              onClick={() => privacyCheck.mutate(false)}
              loading={privacyCheck.isPending}
            >
              <ShieldCheck aria-hidden="true" />
              Run Privacy Check
            </Button>
            <Button
              variant="outline"
              onClick={() => privacyCheck.mutate(true)}
              disabled={privacyCheck.isPending}
            >
              Run full check (~90s)
            </Button>
          </div>
          {privacyCheck.isSuccess ? (
            <p
              className="text-sm"
              role="status"
              data-testid="privacy-check-result"
            >
              {privacyCheck.data.passed
                ? "Passed — your server keeps rows private."
                : "Failed — rows are NOT private on this server. Shortlist will refuse to write."}
            </p>
          ) : null}
          {privacyCheck.isError ? (
            <p className="text-sm text-destructive" role="alert">
              {apiErrorMessage(
                privacyCheck.error,
                "The Privacy Check could not run.",
              )}
            </p>
          ) : null}
        </CardContent>
      </Card>
    </section>
  );
}
