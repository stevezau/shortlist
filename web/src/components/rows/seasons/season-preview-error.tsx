import { Link } from "react-router";

import { Button } from "@/components/ui/button";
import { apiErrorMessage } from "@/lib/api";
import { NO_TMDB_KEY, needsSetup } from "@/lib/season-draft";

const LINK =
  "rounded-sm font-medium text-primary underline-offset-2 hover:underline focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring";

/**
 * Why a season's films couldn't be counted, in the server's words, and what to do: open Settings for a
 * missing TMDB key — in a new tab, so a season half made here isn't lost — or try again for anything
 * that might pass next time (a 502 from TMDB or Plex, a dropped connection).
 */
export function SeasonPreviewError({
  error,
  onRetry,
  compact = false,
}: {
  error: unknown;
  onRetry: () => void;
  /** One small line, for a list entry or a card; the editor's summary has room for a button. */
  compact?: boolean;
}) {
  const message = apiErrorMessage(error, "Couldn’t count the films. Check Shortlist is running, then try again.");
  const setup = needsSetup(error);
  const settingsLink = setup && NO_TMDB_KEY.test(message) && (
    <Link to="/settings#connections" target="_blank" rel="noreferrer" className={LINK}>
      Open Settings in a new tab
    </Link>
  );

  if (compact) {
    return (
      <p className="text-xs text-muted-foreground">
        {message}{" "}
        {setup ? (
          settingsLink
        ) : (
          <button type="button" onClick={onRetry} className={LINK}>
            Retry
          </button>
        )}
      </p>
    );
  }
  return (
    <div className="space-y-3 text-sm">
      <p role="alert">{message}</p>
      {setup ? (
        <p className="text-muted-foreground">
          {settingsLink}
          {settingsLink && " — "}The count runs again when you come back to this tab.
        </p>
      ) : (
        <Button type="button" variant="outline" size="sm" onClick={onRetry}>
          Retry
        </Button>
      )}
    </div>
  );
}
